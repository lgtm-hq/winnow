"""Discovery and removal of empty directory trees.

Removal is bottom-up: a directory is removable only when it holds no files
and every subdirectory is itself removable, so clearing nested leaves can
cascade up to their now-empty parents. Sibling subtrees are processed in
ascending, code-point (str) order. Callers choose whether the root itself
may be removed once everything beneath it is gone.

Commands that confirm a plan with the user scan once with
:func:`scan_empty_directories` and then apply exactly that plan with
:func:`remove_directories`, so nothing discovered after confirmation is
removed. :func:`remove_empty_tree` re-scans and is meant only for trees
winnow itself owns.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from winnow.fs.errors import FileSystemOperationError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "BENIGN_SKIP_REASONS",
    "EmptyDirScan",
    "RemovalResult",
    "find_empty_directories",
    "remove_directories",
    "remove_empty_tree",
    "scan_empty_directories",
]

SKIP_REASON_MISSING = "no longer exists"
SKIP_REASON_NOT_EMPTY = "no longer empty"
SKIP_REASON_PATH_CHANGED = "path changed"
SKIP_REASON_PERMISSION = "permission denied"
SKIP_REASON_NOT_DIRECTORY = "not a directory"
BENIGN_SKIP_REASONS = frozenset({SKIP_REASON_MISSING, SKIP_REASON_NOT_EMPTY})
"""Skip reasons that mean the tree changed harmlessly during confirmation."""

_UNSAFE_PARTS = frozenset({os.curdir, os.pardir})


@dataclass(frozen=True, slots=True)
class EmptyDirScan:
    """Outcome of scanning a tree for removable empty directories.

    Attributes:
        candidates: Removable directories, children before parents.
        unreadable: Directories the walk could not list. They and their
            ancestors are never candidates.
        root_identity: ``(st_dev, st_ino)`` of the scanned root, following
            symlinks, or ``None`` when it could not be read. Pass it to
            :func:`remove_directories` so a root swapped after the scan is
            detected.
    """

    candidates: tuple[Path, ...]
    unreadable: tuple[Path, ...]
    root_identity: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class RemovalResult:
    """Outcome of removing a fixed list of directories.

    Attributes:
        removed: Directories that were removed, in removal order.
        skipped: ``(path, reason)`` pairs for directories left in place.
            Reasons in :data:`BENIGN_SKIP_REASONS` mean the directory vanished
            or gained content; any other reason names a failure.
    """

    removed: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...]


def _is_excluded(
    directory: Path,
    *,
    root: Path,
    patterns: Sequence[str],
) -> bool:
    """Report whether a directory matches any exclude pattern.

    Patterns are matched against both the directory name and its path
    relative to ``root`` (using forward slashes), so ``".git"`` and
    ``"cache/*"`` both work as expected. The directory's ancestors below
    ``root`` are checked too, so everything inside an excluded subtree is
    preserved even though bottom-up processing considers it first.

    Args:
        directory: Directory being considered for removal.
        root: Root directory the walk started from.
        patterns: Glob patterns identifying directories to preserve.

    Returns:
        ``True`` when the directory should be preserved.
    """
    if not patterns:
        return False
    candidates = [directory, *directory.parents]
    for candidate in candidates:
        if candidate == root:
            break
        relative = candidate.relative_to(root).as_posix()
        name = candidate.name
        if any(
            fnmatch(name, pattern) or fnmatch(relative, pattern) for pattern in patterns
        ):
            return True
    return False


def _identity(root: Path) -> tuple[int, int] | None:
    """Return ``root``'s ``(st_dev, st_ino)``, following symlinks.

    Args:
        root: Directory to identify.

    Returns:
        The device and inode numbers, or ``None`` when ``root`` cannot be
        stat'ed.
    """
    try:
        info = os.stat(root)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def scan_empty_directories(
    root: Path,
    *,
    exclude_patterns: Sequence[str] = (),
    include_root: bool = False,
) -> EmptyDirScan:
    """Scan ``root`` for removable empty directories and unreadable ones.

    A directory the walk cannot list is recorded in ``unreadable``. Because
    it is never visited it is not removable, so neither are its ancestors.

    Args:
        root: Directory to search within.
        exclude_patterns: Glob patterns for directories to preserve. An
            excluded directory keeps its ancestors from being removed.
        include_root: When ``True``, ``root`` itself is appended last if it
            holds no files and every child directory was removable.

    Returns:
        The removable directories in ascending sibling-subtree order, with
        children before parents, the directories that could not be read, in
        ascending order, and the identity of the scanned root. Excluded
        directories are not walked, so they are never reported as unreadable.
    """
    root_identity = _identity(root)
    unreadable: list[Path] = []

    def record_unreadable(error: OSError) -> None:
        """Remember a directory ``os.walk`` failed to list.

        Args:
            error: Error raised while listing the directory.
        """
        unreadable.append(Path(error.filename) if error.filename else root)

    removable: set[Path] = set()
    ordered: list[Path] = []
    # Only top-down walks honor changes to subdir_names. Reverse the
    # descending traversal to get ascending sibling subtrees in postorder.
    # Excluded children are pruned so they are never listed, but they stay
    # in the recorded child names so their ancestors are kept.
    walked: list[tuple[Path, list[str], bool]] = []
    for current_path, subdir_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=record_unreadable,
    ):
        current = Path(current_path)
        subdir_names.sort(reverse=True)
        walked.append((current, list(subdir_names), bool(file_names)))
        subdir_names[:] = [
            name
            for name in subdir_names
            if not _is_excluded(current / name, root=root, patterns=exclude_patterns)
        ]
    for current, subdir_names, has_files in reversed(walked):
        if current == root and not include_root:
            continue
        if has_files:
            continue
        child_dirs = (current / name for name in subdir_names)
        if all(child in removable for child in child_dirs):
            removable.add(current)
            ordered.append(current)
    return EmptyDirScan(
        candidates=tuple(ordered),
        unreadable=tuple(sorted(unreadable)),
        root_identity=root_identity,
    )


def find_empty_directories(
    root: Path,
    *,
    exclude_patterns: Sequence[str] = (),
    include_root: bool = False,
) -> list[Path]:
    """Find removable empty directories under ``root``.

    Compatibility wrapper around :func:`scan_empty_directories` that drops
    the list of unreadable directories.

    Args:
        root: Directory to search within.
        exclude_patterns: Glob patterns for directories to preserve. An
            excluded directory keeps its ancestors from being removed.
        include_root: When ``True``, ``root`` itself is appended last if it
            holds no files and every child directory was removable.

    Returns:
        Removable directories in ascending sibling-subtree order, with
        children before parents, making them safe to delete sequentially.
    """
    scan = scan_empty_directories(
        root,
        exclude_patterns=exclude_patterns,
        include_root=include_root,
    )
    return list(scan.candidates)


def _skip_reason(error: OSError) -> str:
    """Name why ``rmdir`` left a directory in place.

    Args:
        error: Error raised by ``rmdir``.

    Returns:
        A short reason for the skipped-directory report.
    """
    if isinstance(error, FileNotFoundError):
        return SKIP_REASON_MISSING
    if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
        return SKIP_REASON_NOT_EMPTY
    if isinstance(error, PermissionError):
        return SKIP_REASON_PERMISSION
    if isinstance(error, NotADirectoryError):
        return SKIP_REASON_NOT_DIRECTORY
    return error.strerror or str(error)


def _relative_parts(directory: Path, root: Path) -> tuple[str, ...]:
    """Split ``directory`` into its components below ``root``.

    Args:
        directory: Candidate directory.
        root: Scan root the candidate was found under.

    Returns:
        The path components from ``root`` down to ``directory``.

    Raises:
        ValueError: If ``directory`` is not strictly below ``root``, or a
            component below ``root`` is ``.`` or ``..``. The comparison is
            lexical, so a ``..`` could otherwise lead outside ``root``.
    """
    parts = directory.relative_to(root).parts if directory.is_relative_to(root) else ()
    if not parts or not _UNSAFE_PARTS.isdisjoint(parts):
        message = f"{directory} is not below {root}"
        raise ValueError(message)
    return parts


def _rmdir_below_descriptor(root_fd: int, parts: tuple[str, ...]) -> str | None:
    """Remove ``parts`` relative to ``root_fd`` without following symlinks.

    Each intermediate component is opened with ``O_NOFOLLOW`` relative to
    its parent descriptor, so a component swapped for a symlink can never
    lead outside the root. Each intermediate descriptor is closed as soon as
    its child is open, so at most two are open besides ``root_fd``.

    Args:
        root_fd: Open descriptor of the scan root.
        parts: Components of the candidate below the root.

    Returns:
        ``None`` when the directory was removed, otherwise a skip reason.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = root_fd
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    return SKIP_REASON_PATH_CHANGED
                return _skip_reason(exc)
            previous_fd, parent_fd = parent_fd, child_fd
            if previous_fd != root_fd:
                os.close(previous_fd)
        return _rmdir_leaf(parent_fd, parts[-1])
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)


def _rmdir_leaf(parent_fd: int, leaf: str) -> str | None:
    """Remove ``leaf`` relative to ``parent_fd`` without following it.

    Args:
        parent_fd: Open descriptor of the leaf's parent.
        leaf: Name of the directory to remove.

    Returns:
        ``None`` when the directory was removed, otherwise a skip reason.
    """
    try:
        os.rmdir(leaf, dir_fd=parent_fd)
    except NotADirectoryError as exc:
        # rmdir never follows a final symlink; name that case precisely.
        try:
            mode = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False).st_mode
        except OSError as stat_exc:
            # The leaf changed again between the two calls.
            return _skip_reason(stat_exc)
        return SKIP_REASON_PATH_CHANGED if stat.S_ISLNK(mode) else _skip_reason(exc)
    except OSError as exc:
        return _skip_reason(exc)
    return None


def _is_junction(path: Path) -> bool:
    """Report whether ``path`` is a directory junction.

    Args:
        path: Path to check.

    Returns:
        ``True`` for a junction; always ``False`` before Python 3.12, which
        cannot detect one.
    """
    if sys.version_info >= (3, 12):
        return os.path.isjunction(path)
    return False  # pragma: no cover - only reached before Python 3.12


def _path_changed(root: Path, parts: tuple[str, ...]) -> bool:
    """Report whether a component below ``root`` no longer matches the plan.

    A component has changed when it is a symlink or a junction, or, for an
    intermediate component, when it is no longer a directory. A missing
    component is left for ``rmdir`` to report.

    Args:
        root: Scan root the candidate was found under.
        parts: Components of the candidate below the root, leaf included.

    Returns:
        ``True`` when the candidate must be skipped as ``"path changed"``.
    """
    current = root
    for index, part in enumerate(parts, start=1):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode) or _is_junction(current):
            return True
        if index < len(parts) and not stat.S_ISDIR(mode):
            return True
    return False


def _rmdir_path(directory: Path) -> str | None:
    """Remove ``directory`` with ``Path.rmdir``.

    Args:
        directory: Directory to remove.

    Returns:
        ``None`` when the directory was removed, otherwise a skip reason.
    """
    try:
        directory.rmdir()
    except OSError as exc:
        return _skip_reason(exc)
    return None


def _supports_dir_fd() -> bool:
    """Report whether descriptor-relative removal is fully available.

    Returns:
        ``True`` when ``O_DIRECTORY`` and ``O_NOFOLLOW`` exist, ``open``,
        ``rmdir`` and ``stat`` accept ``dir_fd``, and ``stat`` accepts
        ``follow_symlinks``.
    """
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def _identity_mismatch(
    info: os.stat_result,
    root_identity: tuple[int, int] | None,
) -> bool:
    """Report whether ``info`` names a different directory than the scan.

    Args:
        info: Current status of the root, following symlinks.
        root_identity: ``(st_dev, st_ino)`` recorded at scan time, or
            ``None`` when there is nothing to compare.

    Returns:
        ``True`` when an identity was recorded and it differs.
    """
    return root_identity is not None and (info.st_dev, info.st_ino) != root_identity


def _open_root(root: Path, root_identity: tuple[int, int] | None) -> int | str:
    """Open the scan root as a directory descriptor and check its identity.

    The root is opened following symlinks, so a root the caller reached
    through a symlink keeps working while it leads to the scanned directory.

    Args:
        root: Scan root to open.
        root_identity: ``(st_dev, st_ino)`` recorded at scan time, or
            ``None`` to skip the identity check.

    Returns:
        The open descriptor, or the skip reason when it cannot be opened or
        is no longer the scanned directory.
    """
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        return _skip_reason(exc)
    try:
        info = os.fstat(root_fd)
    except OSError as exc:
        os.close(root_fd)
        return _skip_reason(exc)
    if _identity_mismatch(info, root_identity):
        os.close(root_fd)
        return SKIP_REASON_PATH_CHANGED
    return root_fd


def _check_root_path(root: Path, root_identity: tuple[int, int] | None) -> str | None:
    """Check the scan root by path where descriptors are unavailable.

    Args:
        root: Scan root, followed if it is a symlink.
        root_identity: ``(st_dev, st_ino)`` recorded at scan time, or
            ``None`` to skip the identity check.

    Returns:
        ``None`` when the root may be used, otherwise a skip reason.
    """
    if root_identity is None:
        return None
    try:
        info = os.stat(root)
    except OSError as exc:
        return _skip_reason(exc)
    return SKIP_REASON_PATH_CHANGED if _identity_mismatch(info, root_identity) else None


def _remove_below_root(
    directory: Path,
    parts: tuple[str, ...],
    *,
    root: Path,
    root_fd: int | str | None,
) -> str | None:
    """Remove ``directory`` below ``root`` without following new symlinks.

    Args:
        directory: Candidate directory, strictly below ``root``.
        parts: Components of ``directory`` below ``root``.
        root: Scan root the candidate was found under.
        root_fd: Open root descriptor, the reason the root cannot be used,
            or ``None`` where descriptors are unsupported and the root
            check passed.

    Returns:
        ``None`` when the directory was removed, otherwise a skip reason.
    """
    if isinstance(root_fd, int):
        return _rmdir_below_descriptor(root_fd, parts)
    if root_fd is not None:
        return root_fd
    if _path_changed(root, parts):
        return SKIP_REASON_PATH_CHANGED
    return _rmdir_path(directory)


def remove_directories(
    candidates: Sequence[Path],
    *,
    root: Path | None = None,
    root_identity: tuple[int, int] | None = None,
) -> RemovalResult:
    """Remove exactly ``candidates``, in the given order, with ``rmdir``.

    Pass children before parents, as :func:`scan_empty_directories`
    returns them. A directory that cannot be removed is skipped and
    reported with its reason rather than raised, and removal continues with
    the rest; a parent of a skipped child is then skipped too, because it
    is no longer empty.

    With ``root``, only directories really below the scanned root are
    removed. Every candidate is checked before the first removal: one that
    is not strictly below ``root``, or that has a ``.`` or ``..`` component,
    raises ``ValueError`` and nothing is removed. With ``root_identity`` as
    well, the root must still be the directory that was scanned; otherwise
    every candidate is skipped as ``"path changed"``. The root itself is
    followed if it is a symlink, so a root reached through one keeps
    working while it leads to the scanned directory.

    Where ``open``, ``rmdir`` and ``stat`` fully support descriptors, the
    root is opened once, each intermediate component is opened with
    ``O_NOFOLLOW`` relative to its parent, and the leaf is removed relative
    to its parent's descriptor, so a component swapped for a symlink is
    never followed. Elsewhere the check is best effort: just before each
    removal, a candidate with a symlink or junction component below
    ``root``, or with an intermediate component that is no longer a
    directory, is refused, but a swap between that check and ``rmdir`` is
    not prevented. Either way a refused candidate is skipped as
    ``"path changed"``.

    Args:
        candidates: Directories to remove, children before parents.
        root: Scan root the candidates were found under. Without it each
            candidate is removed by path.
        root_identity: ``(st_dev, st_ino)`` of the root at scan time, as in
            :attr:`EmptyDirScan.root_identity`. Used only with ``root``;
            ``None`` skips the root identity check.

    Returns:
        The removed directories and the skipped ones with their reasons.

    Raises:
        ValueError: If ``root`` is given and a candidate is not strictly
            below it or has a ``.`` or ``..`` component.
    """
    planned = [
        (directory, () if root is None else _relative_parts(directory, root))
        for directory in candidates
    ]
    removed: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    with ExitStack() as stack:
        root_fd: int | str | None = None
        if root is not None and planned:
            if _supports_dir_fd():
                root_fd = _open_root(root, root_identity)
                if isinstance(root_fd, int):
                    stack.callback(os.close, root_fd)
            else:
                root_fd = _check_root_path(root, root_identity)
        for directory, parts in planned:
            if root is None:
                reason = _rmdir_path(directory)
            else:
                reason = _remove_below_root(
                    directory,
                    parts,
                    root=root,
                    root_fd=root_fd,
                )
            if reason is None:
                removed.append(directory)
            else:
                skipped.append((directory, reason))
    return RemovalResult(removed=tuple(removed), skipped=tuple(skipped))


def remove_empty_tree(
    root: Path,
    *,
    exclude_patterns: Sequence[str] = (),
    include_root: bool = True,
) -> list[Path]:
    """Remove empty directories in ascending sibling order, children first.

    Args:
        root: Directory tree to prune.
        exclude_patterns: Glob patterns for directories to preserve. An
            excluded directory keeps its ancestors from being removed.
        include_root: When ``True``, ``root`` itself is removed once it is
            empty; when ``False`` it is always preserved.

    Returns:
        The directories that were removed, in removal order.

    Raises:
        FileSystemOperationError: If a directory cannot be removed, for
            example because it gained a file between discovery and removal.
            Directories removed before the failure stay removed.
    """
    removed: list[Path] = []
    for directory in find_empty_directories(
        root,
        exclude_patterns=exclude_patterns,
        include_root=include_root,
    ):
        try:
            directory.rmdir()
        except OSError as exc:
            raise FileSystemOperationError(
                "failed to remove empty directory",
                operation="remove_empty_tree",
                file_path=directory,
            ) from exc
        removed.append(directory)
    return removed
