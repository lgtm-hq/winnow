"""Backup helpers for destructive filesystem operations."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from winnow.fs._path_ops import copy_path, path_exists, remove_path
from winnow.fs.backup_options import BackupOptions


def create_backup(
    path: Path,
    *,
    options: BackupOptions | None = None,
) -> Path | None:
    """Create a persistent backup of an existing filesystem path.

    Args:
        path: File, directory, or symlink to back up.
        options: Backup behavior configuration.

    Returns:
        Path to the backup, or ``None`` when backups are disabled or the source
        path does not exist.
    """
    backup_options = options or BackupOptions()
    if not backup_options.enabled or not path_exists(path):
        return None

    backup_directory = backup_options.directory or path.parent / ".winnow-backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = _unique_backup_path(
        path=path,
        backup_directory=backup_directory,
        suffix=backup_options.suffix,
    )
    copy_path(source=path, destination=backup_path)
    return backup_path


def restore_backup(
    backup_path: Path,
    destination: Path,
) -> None:
    """Restore a backup to a destination path.

    Args:
        backup_path: Existing backup path to restore.
        destination: Destination path to replace with the backup contents.
    """
    if path_exists(destination):
        remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copy_path(source=backup_path, destination=destination)


def _unique_backup_path(
    *,
    path: Path,
    backup_directory: Path,
    suffix: str,
) -> Path:
    """Return a backup path that does not already exist."""
    while True:
        candidate = backup_directory / f"{path.name}.{uuid4().hex}{suffix}"
        if not path_exists(candidate):
            return candidate
