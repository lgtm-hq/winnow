"""Shared low-level filesystem path helpers.

These utilities are shared infrastructure used by both :mod:`winnow.fs.backup`
and :mod:`winnow.fs.transaction`. Keeping them in one dedicated module gives
both callers a single stable import location instead of reaching across modules
for private helpers.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4


def copy_path(
    *,
    source: Path,
    destination: Path,
) -> None:
    """Copy a file, directory, or symlink preserving metadata.

    Mode and timestamps are preserved via ``copy2``/``copytree``; file
    ownership is not.
    """
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True)
        return
    shutil.copy2(source, destination, follow_symlinks=False)


def path_exists(path: Path) -> bool:
    """Return whether a path exists, including broken symlinks."""
    return path.exists() or path.is_symlink()


def remove_path(path: Path) -> None:
    """Remove a file, directory, or symlink path."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def temporary_path(path: Path) -> Path:
    """Return a unique staging path next to a destination path.

    Args:
        path: Destination path the staging path should sit beside.

    Returns:
        Hidden sibling path that does not already exist.
    """
    while True:
        candidate = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        if not path_exists(candidate):
            return candidate


def sync_path(path: Path) -> None:
    """Flush a staged file or directory tree to stable storage, best effort.

    Called on staged content before an atomic replace so a crash cannot leave
    a successfully renamed destination with unwritten data. Directory trees are
    synced file by file, then their directory entries are fsynced bottom-up
    (deepest first) before the staged root directory, so the tree structure is
    durable and not just the file contents; symlinks need no syncing.
    """
    if path.is_symlink():
        return
    if path.is_file():
        _sync_file(path)
        return
    if path.is_dir():
        nested_directories: list[Path] = []
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                continue
            if child.is_file():
                _sync_file(child)
            elif child.is_dir():
                nested_directories.append(child)
        for directory in sorted(
            nested_directories,
            key=lambda entry: len(entry.parts),
            reverse=True,
        ):
            sync_directory(directory)
        sync_directory(path)


def sync_directory(path: Path) -> None:
    """Flush a directory entry so renames inside it are durable, best effort.

    POSIX requires fsyncing the parent directory for a rename to be durable.
    Platforms that cannot open directories for reading (e.g. Windows) are
    skipped silently.
    """
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:  # pragma: no cover - platform-specific
        pass
    finally:
        os.close(directory_fd)


def _sync_file(path: Path) -> None:
    """Flush one regular file's contents to stable storage, best effort."""
    try:
        file_fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - permission-specific
        return
    try:
        os.fsync(file_fd)
    except OSError:  # pragma: no cover - platform-specific
        pass
    finally:
        os.close(file_fd)
