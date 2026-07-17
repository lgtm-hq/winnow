"""Backup helpers for destructive filesystem operations."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from winnow.fs._path_ops import copy_path, path_exists, remove_path
from winnow.fs.backup_options import BackupOptions
from winnow.fs.errors import FileSystemOperationError

CleanupStep = Callable[[], object]


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
    except (OSError, shutil.Error) as error:
        _run_cleanups(error, [lambda: _cleanup_path(staging_path)])
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
    created_parent_dirs: list[Path] = []
    staging_path: Path | None = None
    tombstone: Path | None = None
    try:
        created_parent_dirs = _create_parent_directories(destination.parent)
        staging_path = _staging_path(destination)
        copy_path(source=backup_path, destination=staging_path)
        if path_exists(destination):
            tombstone = _staging_path(destination)
            destination.replace(tombstone)
        staging_path.replace(destination)
    except (OSError, shutil.Error) as error:
        operation_error = FileSystemOperationError(
            "failed to restore filesystem backup",
            operation="fs.restore",
            file_path=destination,
            details={"error": str(error)},
        )
        cleanups: list[CleanupStep] = []
        if staging_path is not None:
            cleanups.append(lambda: _cleanup_path(staging_path))
        if tombstone is not None:
            cleanups.append(
                lambda: _restore_tombstone(
                    tombstone=tombstone,
                    destination=destination,
                )
            )
        cleanups.append(lambda: _cleanup_created_directories(created_parent_dirs))
        _run_cleanups(operation_error, cleanups)
        raise operation_error from error
    if tombstone is not None and path_exists(tombstone):
        remove_path(tombstone)


def _cleanup_created_directories(created_dirs: list[Path]) -> None:
    """Remove directories created for a failed restore, leaf first."""
    errors: list[OSError] = []
    for created_dir in reversed(created_dirs):
        try:
            created_dir.rmdir()
        except FileNotFoundError:
            continue
        except OSError as cleanup_error:
            errors.append(cleanup_error)
    if errors:
        for secondary_error in errors[1:]:
            errors[0].add_note(f"parent cleanup failed: {secondary_error}")
        raise errors[0]


def _cleanup_path(path: Path) -> None:
    """Remove a path when it exists."""
    if path_exists(path):
        remove_path(path)


def _create_parent_directories(parent: Path) -> list[Path]:
    """Create missing destination parents and return only newly created dirs."""
    created_dirs: list[Path] = []
    for missing_dir in _missing_directories(parent):
        missing_dir.mkdir()
        created_dirs.append(missing_dir)
    return created_dirs


def _missing_directories(path: Path) -> list[Path]:
    """Return missing directories from highest parent to leaf."""
    missing_dirs: list[Path] = []
    cursor = path
    while not path_exists(cursor):
        missing_dirs.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    missing_dirs.reverse()
    return missing_dirs


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


def _restore_tombstone(
    *,
    tombstone: Path,
    destination: Path,
) -> None:
    """Restore a tombstoned destination if the tombstone was created."""
    if not path_exists(tombstone):
        return
    errors: list[Exception] = []
    if path_exists(destination):
        try:
            remove_path(destination)
        except (OSError, shutil.Error) as cleanup_error:
            errors.append(cleanup_error)
    try:
        tombstone.replace(destination)
    except OSError as cleanup_error:
        errors.append(cleanup_error)
    if errors:
        for secondary_error in errors[1:]:
            errors[0].add_note(f"tombstone restore failed: {secondary_error}")
        raise errors[0]


def _run_cleanups(error: BaseException, cleanups: list[CleanupStep]) -> None:
    """Run every cleanup and attach cleanup failures to the primary error."""
    for cleanup in cleanups:
        try:
            cleanup()
        except (OSError, shutil.Error) as cleanup_error:
            error.add_note(f"cleanup failed: {cleanup_error}")
            for note in getattr(cleanup_error, "__notes__", ()):
                error.add_note(f"cleanup detail: {note}")


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
