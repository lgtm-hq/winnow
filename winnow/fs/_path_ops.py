"""Shared low-level filesystem path helpers.

These utilities are shared infrastructure used by both :mod:`winnow.fs.backup`
and :mod:`winnow.fs.transaction`. Keeping them in one dedicated module gives
both callers a single stable import location instead of reaching across modules
for private helpers.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def copy_path(
    *,
    source: Path,
    destination: Path,
) -> None:
    """Copy a file, directory, or symlink preserving metadata."""
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
