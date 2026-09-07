"""Discovery and removal of empty directory trees.

Removal is bottom-up: a directory is removable only when it holds no files
and every subdirectory is itself removable, so clearing nested leaves can
cascade up to their now-empty parents. Callers choose whether the root itself
may be removed once everything beneath it is gone.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from winnow.fs.errors import FileSystemOperationError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["find_empty_directories", "remove_empty_tree"]


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
    preserved even though ``os.walk(topdown=False)`` visits it first.

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


def find_empty_directories(
    root: Path,
    *,
    exclude_patterns: Sequence[str] = (),
    include_root: bool = False,
) -> list[Path]:
    """Find removable empty directories under ``root``.

    Args:
        root: Directory to search within.
        exclude_patterns: Glob patterns for directories to preserve. An
            excluded directory keeps its ancestors from being removed.
        include_root: When ``True``, ``root`` itself is appended last if it
            holds no files and every child directory was removable.

    Returns:
        Removable directories ordered so that children precede parents,
        making them safe to delete sequentially.
    """
    removable: set[Path] = set()
    ordered: list[Path] = []
    for current_path, subdir_names, file_names in os.walk(root, topdown=False):
        current = Path(current_path)
        if current == root and not include_root:
            continue
        if file_names:
            continue
        if _is_excluded(current, root=root, patterns=exclude_patterns):
            continue
        child_dirs = (current / name for name in subdir_names)
        if all(child in removable for child in child_dirs):
            removable.add(current)
            ordered.append(current)
    return ordered


def remove_empty_tree(
    root: Path,
    *,
    exclude_patterns: Sequence[str] = (),
    include_root: bool = True,
) -> list[Path]:
    """Remove every empty directory under ``root``, children before parents.

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
