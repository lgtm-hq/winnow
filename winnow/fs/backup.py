"""Backup helpers for destructive filesystem operations."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from winnow.fs._path_ops import copy_path, path_exists, remove_path
from winnow.fs.backup_options import BackupOptions
from winnow.fs.errors import FileSystemOperationError


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
    _reject_nested_backup_directory(path=path, backup_directory=backup_directory)
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = _unique_backup_path(
        path=path,
        backup_directory=backup_directory,
        suffix=backup_options.suffix,
    )
    staging_path = backup_directory / f".{backup_path.name}.{uuid4().hex}.partial"
    try:
        copy_path(source=path, destination=staging_path)
        staging_path.replace(backup_path)
    except (OSError, shutil.Error):
        if path_exists(staging_path):
            remove_path(staging_path)
        raise
    return backup_path


def restore_backup(
    backup_path: Path,
    destination: Path,
) -> None:
    """Restore a backup to a destination path.

    Args:
        backup_path: Existing backup path to restore.
        destination: Destination path to replace with the backup contents.

    Raises:
        FileSystemOperationError: When the restore cannot be applied. The prior
            destination is preserved when restoration fails.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_path = _staging_path(destination)
    tombstone: Path | None = None
    try:
        copy_path(source=backup_path, destination=staging_path)
        if path_exists(destination):
            tombstone = _staging_path(destination)
            destination.replace(tombstone)
        staging_path.replace(destination)
    except (OSError, shutil.Error) as error:
        if path_exists(staging_path):
            remove_path(staging_path)
        if tombstone is not None and path_exists(tombstone):
            if path_exists(destination):
                remove_path(destination)
            tombstone.replace(destination)
        raise FileSystemOperationError(
            "failed to restore filesystem backup",
            operation="fs.restore",
            file_path=destination,
            details={"error": str(error)},
        ) from error
    if tombstone is not None and path_exists(tombstone):
        remove_path(tombstone)


def _reject_nested_backup_directory(
    *,
    path: Path,
    backup_directory: Path,
) -> None:
    """Reject a backup directory located inside the source directory.

    Copying a directory into a backup location nested inside that directory would
    make ``copytree`` recurse into the backup it is creating.

    Raises:
        FileSystemOperationError: When the backup directory is inside the source.
    """
    if not path.is_dir() or path.is_symlink():
        return
    source_root = path.resolve()
    backup_root = backup_directory.resolve()
    if backup_root == source_root or backup_root.is_relative_to(source_root):
        raise FileSystemOperationError(
            "backup directory must not be nested inside the source path",
            operation="fs.backup",
            file_path=path,
            details={"backup_directory": str(backup_directory)},
        )


def _staging_path(path: Path) -> Path:
    """Return a unique staging path next to a destination path."""
    while True:
        candidate = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        if not path_exists(candidate):
            return candidate


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
