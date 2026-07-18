"""Copy operation: forward apply, rollback, and replace-staging helpers.

Patchable filesystem helpers (``copy_path``, ``_cleanup_path``,
``_restore_tombstone``, ...) are resolved through
:mod:`winnow.fs.transaction` at call time so failure-injection tests keep a
single stable patch point.
"""

from __future__ import annotations

import errno
import shutil
from pathlib import Path

# Circular by design: winnow.fs.transaction imports this module and hosts the
# patchable helper bindings. Attributes are only resolved at call time, after
# both modules have fully initialized.
from winnow.fs import transaction as _txn
from winnow.fs._operation import (
    AppliedOperation,
    RollbackStep,
    operation_error,
    validate_destination_parent,
    validate_source,
)
from winnow.fs._path_ops import (
    path_exists,
    sync_directory,
    sync_path,
    temporary_path,
)
from winnow.fs.backup import create_backup
from winnow.fs.backup_options import BackupOptions
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation


def apply_copy(
    *,
    source: Path,
    destination: Path,
    backup_options: BackupOptions,
) -> AppliedOperation:
    """Atomically copy a path into place.

    Args:
        source: Existing file, directory, or symlink to copy.
        destination: Destination path to create or replace.
        backup_options: Backup configuration for a replaced destination.

    Returns:
        Applied operation with its log entry and lifecycle hooks.

    Raises:
        FileSystemOperationError: When the copy cannot be applied.
    """
    temp_path = temporary_path(destination)
    destination_tombstone: Path | None = None
    destination_renamed = False
    backups: list[Path] = []

    try:
        _validate_copy(source=source, destination=destination)
        _stage_source_copy(source=source, temp_path=temp_path)
        if path_exists(destination):
            destination_tombstone, destination_renamed = _stage_existing_destination(
                destination=destination,
                backup_options=backup_options,
                backups=backups,
            )
        _execute_replace(temp_path=temp_path, destination=destination)
    except (OSError, shutil.Error) as error:
        raise _copy_failure(
            source=source,
            destination=destination,
            temp_path=temp_path,
            destination_tombstone=destination_tombstone,
            destination_renamed=destination_renamed,
            backups=backups,
            error=error,
        ) from error

    log = OperationLog(
        operation=FileOperation.COPY,
        source=source,
        destination=destination,
        backups=tuple(backups),
        created_paths=(destination,),
    )
    return AppliedOperation(
        log=log,
        rollback=lambda: rollback_copy(
            destination=destination,
            destination_tombstone=destination_tombstone,
        ),
        commit=lambda: _txn._commit_tombstones(destination_tombstone),
    )


def rollback_copy(
    *,
    destination: Path,
    destination_tombstone: Path | None,
) -> None:
    """Roll back a completed copy operation.

    Args:
        destination: Destination path created by the copy.
        destination_tombstone: Staged prior destination content, if any.
    """
    _txn._cleanup_path(destination)
    if destination_tombstone is not None:
        _txn._restore_tombstone(
            tombstone=destination_tombstone,
            destination=destination,
        )


def reject_recursive_directory_copy(
    *,
    source: Path,
    destination: Path,
    operation: FileOperation,
) -> None:
    """Reject copying a real directory into a destination inside its own tree.

    Staging happens next to the destination, so a destination that resolves
    beneath the source would create the temporary copy within the source and
    recurse into it, consuming disk without bound. Symlinked and non-directory
    sources cannot recurse and are left untouched.

    Args:
        source: Source path about to be copied.
        destination: Destination path the copy would create.
        operation: Operation being validated, used in the error message.

    Raises:
        OSError: With ``errno.EINVAL`` when the destination resolves inside the
            source directory tree.
    """
    if not source.is_dir() or source.is_symlink():
        return
    resolved_source = source.resolve()
    resolved_destination = destination.resolve()
    if resolved_source in resolved_destination.parents:
        raise OSError(
            errno.EINVAL,
            f"cannot {operation.value} directory into its own subtree: "
            f"{source} -> {destination}",
        )


def stage_destination_for_replace(destination: Path) -> tuple[Path, bool]:
    """Stage an existing destination so it can be replaced atomically.

    Regular files and symlinks are preserved by copying them aside, so the
    destination itself never disappears before ``Path.replace`` atomically
    overwrites it. Real directories cannot be replaced atomically, so they are
    renamed aside instead, which briefly removes the destination.

    Args:
        destination: Existing destination path about to be replaced.

    Returns:
        Tombstone path and whether the destination was renamed rather than
        copied.
    """
    tombstone = temporary_path(destination)
    if destination.is_dir() and not destination.is_symlink():
        destination.replace(tombstone)
        return tombstone, True
    try:
        _txn.copy_path(source=destination, destination=tombstone)
    except (OSError, shutil.Error) as error:
        try:
            _txn._cleanup_path(tombstone)
        except (OSError, shutil.Error) as cleanup_error:
            error.add_note(f"cleanup failed: {cleanup_error}")
        raise
    return tombstone, False


def _copy_failure(
    *,
    source: Path,
    destination: Path,
    temp_path: Path,
    destination_tombstone: Path | None,
    destination_renamed: bool,
    backups: list[Path],
    error: OSError | shutil.Error,
) -> Exception:
    """Build the copy failure error after running staging cleanups.

    Args:
        source: Source path of the failed copy.
        destination: Destination path of the failed copy.
        temp_path: Staging path holding the partial copy.
        destination_tombstone: Staged prior destination content, if any.
        destination_renamed: Whether the destination was renamed aside.
        backups: Backups created before the failure.
        error: Underlying filesystem error.

    Returns:
        Structured operation error annotated with any cleanup failures.
    """
    failure = operation_error(
        operation=FileOperation.COPY,
        path=source,
        error=error,
    )
    cleanups: list[RollbackStep] = [lambda: _txn._cleanup_path(temp_path)]
    if destination_tombstone is not None:
        tombstone = destination_tombstone
        if destination_renamed:
            cleanups.append(
                lambda: _txn._restore_tombstone(
                    tombstone=tombstone,
                    destination=destination,
                ),
            )
        else:
            cleanups.append(lambda: _txn._cleanup_path(tombstone))
    cleanups.append(lambda: _txn._cleanup_backups(backups))
    _txn._run_cleanups(failure, cleanups)
    return failure


def _execute_replace(
    *,
    temp_path: Path,
    destination: Path,
) -> None:
    """Atomically replace the destination with the staged copy.

    Args:
        temp_path: Fully staged and synced copy of the source.
        destination: Destination path to replace.
    """
    temp_path.replace(destination)
    sync_directory(destination.parent)


def _stage_existing_destination(
    *,
    destination: Path,
    backup_options: BackupOptions,
    backups: list[Path],
) -> tuple[Path, bool]:
    """Back up and stage an existing destination before replacing it.

    Args:
        destination: Existing destination path about to be replaced.
        backup_options: Backup configuration for the replaced destination.
        backups: Backup accumulator the created backup is appended to.

    Returns:
        Tombstone path and whether the destination was renamed rather than
        copied.
    """
    backup_path = create_backup(destination, options=backup_options)
    if backup_path is not None:
        backups.append(backup_path)
    return stage_destination_for_replace(destination)


def _stage_source_copy(
    *,
    source: Path,
    temp_path: Path,
) -> None:
    """Copy the source into the staging path and flush it to stable storage.

    Args:
        source: Source path to copy.
        temp_path: Staging path next to the destination.
    """
    _txn.copy_path(source=source, destination=temp_path)
    sync_path(temp_path)


def _validate_copy(
    *,
    source: Path,
    destination: Path,
) -> None:
    """Validate a copy request before touching the filesystem.

    Args:
        source: Source path to copy.
        destination: Destination path to create or replace.

    Raises:
        FileNotFoundError: When the source or destination parent is missing.
        OSError: When the destination resolves inside the source tree.
    """
    validate_source(source=source, operation=FileOperation.COPY)
    validate_destination_parent(destination)
    reject_recursive_directory_copy(
        source=source,
        destination=destination,
        operation=FileOperation.COPY,
    )
