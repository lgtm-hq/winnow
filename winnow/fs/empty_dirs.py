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
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from winnow.fs.errors import FileSystemOperationError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "EmptyDirScan",
    "RemovalResult",
    "find_empty_directories",
    "remove_directories",
    "remove_empty_tree",
    "scan_empty_directories",
]

SKIP_REASON_MISSING = "no longer exists"
SKIP_REASON_NOT_EMPTY = "no longer empty"


@dataclass(frozen=True, slots=True)
class EmptyDirScan:
    """Outcome of scanning a tree for removable empty directories.

    Attributes:
        candidates: Removable directories, children before parents.
        unreadable: Directories the walk could not list. They and their
            ancestors are never candidates.
    """

    candidates: tuple[Path, ...]
    unreadable: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class RemovalResult:
    """Outcome of removing a fixed list of directories.

    Attributes:
        removed: Directories that were removed, in removal order.
        skipped: ``(path, reason)`` pairs for directories left in place
            because they vanished or gained content before removal.
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
        children before parents, and the directories that could not be read.
    """
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
    walked: list[tuple[Path, list[str], bool]] = []
    for current_path, subdir_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=record_unreadable,
    ):
        subdir_names.sort(reverse=True)
        walked.append((Path(current_path), subdir_names, bool(file_names)))
    for current, subdir_names, has_files in reversed(walked):
        if current == root and not include_root:
            continue
        if has_files:
            continue
        if _is_excluded(current, root=root, patterns=exclude_patterns):
            continue
        child_dirs = (current / name for name in subdir_names)
        if all(child in removable for child in child_dirs):
            removable.add(current)
            ordered.append(current)
    return EmptyDirScan(candidates=tuple(ordered), unreadable=tuple(unreadable))


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


def remove_directories(candidates: Sequence[Path]) -> RemovalResult:
    """Remove exactly ``candidates``, in the given order, with ``rmdir``.

    Pass children before parents, as :func:`scan_empty_directories`
    returns them. A path that no longer exists or is no longer empty is
    skipped and reported rather than raised; a parent of a skipped child
    is then skipped too, because it is no longer empty.

    Args:
        candidates: Directories to remove, children before parents.

    Returns:
        The removed directories and the skipped ones with their reasons.

    Raises:
        FileSystemOperationError: If ``rmdir`` fails for any other reason,
            such as missing permissions. Directories removed before the
            failure stay removed.
    """
    removed: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for directory in candidates:
        try:
            directory.rmdir()
        except FileNotFoundError:
            skipped.append((directory, SKIP_REASON_MISSING))
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                skipped.append((directory, SKIP_REASON_NOT_EMPTY))
                continue
            raise FileSystemOperationError(
                "failed to remove empty directory",
                operation="remove_directories",
                file_path=directory,
            ) from exc
        else:
            removed.append(directory)
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
