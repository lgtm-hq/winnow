"""Move operation: forward apply, rollback, and replace staging.

Patchable filesystem helpers (``copy_path``, ``_cleanup_path``,
``_restore_tombstone``, ...) are resolved through
:mod:`winnow.fs.transaction` at call time so failure-injection tests keep a
single stable patch point.
"""

from __future__ import annotations

import errno
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Circular by design: winnow.fs.transaction imports this module and hosts the
# patchable helper bindings. Attributes are only resolved at call time, after
# both modules have fully initialized.
from winnow.fs import transaction as _txn
from winnow.fs._copy import (
    reject_recursive_directory_copy,
    stage_destination_for_replace,
)
from winnow.fs._operation import (
    AppliedOperation,
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


@dataclass(slots=True)
class _MoveStaging:
    """Mutable staging state accumulated while applying a move.

    Attributes:
        temp_path: Staging path next to the destination.
        backups: Backups created for an overwritten destination.
        destination_tombstone: Staged prior destination content, if any.
        destination_renamed: Whether the destination was renamed aside rather
            than copied.
        destination_replaced: Whether the destination was atomically replaced.
        source_tombstone: Staged source path for the copy fallback, if any.
        source_moved_to_temp: Whether the source was renamed into staging.
    """

    temp_path: Path
    backups: list[Path] = field(default_factory=list)
    destination_tombstone: Path | None = None
    destination_renamed: bool = False
    destination_replaced: bool = False
    source_tombstone: Path | None = None
    source_moved_to_temp: bool = False


def apply_move(
    *,
    source: Path,
    destination: Path,
    backup_options: BackupOptions,
) -> AppliedOperation:
    """Atomically move a path into place.

    Only an overwritten destination is backed up: a plain rename destroys
    nothing (rollback restores it by renaming back), so backing up the
    source would double I/O for every move.

    Args:
        source: Existing file, directory, or symlink to move.
        destination: Destination path to create or replace.
        backup_options: Backup configuration for a replaced destination.

    Returns:
        Applied operation with its log entry and lifecycle hooks.

    Raises:
        FileSystemOperationError: When the move cannot be applied.
    """
    staging = _MoveStaging(temp_path=temporary_path(destination))

    try:
        _validate_move(source=source, destination=destination)
        if path_exists(destination):
            _stage_existing_destination(
                destination=destination,
                backup_options=backup_options,
                staging=staging,
            )
        _stage_source_into_temp(
            source=source,
            destination=destination,
            staging=staging,
        )
        _execute_replace(source=source, destination=destination, staging=staging)
    except (OSError, shutil.Error) as error:
        raise _move_failure(
            source=source,
            destination=destination,
            staging=staging,
            error=error,
        ) from error

    log = OperationLog(
        operation=FileOperation.MOVE,
        source=source,
        destination=destination,
        backups=tuple(staging.backups),
        created_paths=(destination,),
    )
    return AppliedOperation(
        log=log,
        rollback=lambda: rollback_move(
            source=source,
            destination=destination,
            staging=staging,
        ),
        commit=lambda: _txn._commit_tombstones(
            staging.destination_tombstone,
            staging.source_tombstone,
        ),
    )


def rollback_move(
    *,
    source: Path,
    destination: Path,
    staging: _MoveStaging,
) -> None:
    """Roll back a completed or partially completed move operation.

    Args:
        source: Source path of the move.
        destination: Destination path of the move.
        staging: Staging state accumulated while applying the move.
    """
    if staging.source_moved_to_temp:
        if (
            staging.destination_replaced
            and path_exists(destination)
            and not path_exists(source)
        ):
            destination.replace(source)
        elif path_exists(staging.temp_path) and not path_exists(source):
            staging.temp_path.replace(source)
        # When the source exists again despite the move, leave the moved data
        # in place rather than discarding it.
    else:
        if staging.destination_replaced:
            _txn._cleanup_path(destination)
        _txn._cleanup_path(staging.temp_path)
        if staging.source_tombstone is not None:
            _txn._restore_tombstone(
                tombstone=staging.source_tombstone,
                destination=source,
            )

    if staging.destination_tombstone is None:
        return
    if staging.destination_renamed or staging.destination_replaced:
        _txn._restore_tombstone(
            tombstone=staging.destination_tombstone,
            destination=destination,
        )
    else:
        # The destination was preserved in place (copy staging) and never
        # replaced, so only the staged copy needs to be discarded.
        _txn._cleanup_path(staging.destination_tombstone)


def _execute_replace(
    *,
    source: Path,
    destination: Path,
    staging: _MoveStaging,
) -> None:
    """Replace the destination with staged content and retire the source.

    Args:
        source: Source path of the move.
        destination: Destination path to replace.
        staging: Staging state accumulated while applying the move.
    """
    staging.temp_path.replace(destination)
    staging.destination_replaced = True
    sync_directory(destination.parent)

    if not staging.source_moved_to_temp:
        staging.source_tombstone = temporary_path(source)
        source.replace(staging.source_tombstone)

    if source.parent != destination.parent:
        sync_directory(source.parent)


def _move_failure(
    *,
    source: Path,
    destination: Path,
    staging: _MoveStaging,
    error: OSError | shutil.Error,
) -> Exception:
    """Build the move failure error after rolling back staged state.

    Args:
        source: Source path of the failed move.
        destination: Destination path of the failed move.
        staging: Staging state accumulated before the failure.
        error: Underlying filesystem error.

    Returns:
        Structured operation error annotated with any cleanup failures.
    """
    failure = operation_error(
        operation=FileOperation.MOVE,
        path=source,
        error=error,
    )
    _txn._run_cleanups(
        failure,
        [
            lambda: rollback_move(
                source=source,
                destination=destination,
                staging=staging,
            ),
            lambda: _txn._cleanup_backups(staging.backups),
        ],
    )
    return failure


def _stage_existing_destination(
    *,
    destination: Path,
    backup_options: BackupOptions,
    staging: _MoveStaging,
) -> None:
    """Back up and stage an existing destination before replacing it.

    Args:
        destination: Existing destination path about to be replaced.
        backup_options: Backup configuration for the replaced destination.
        staging: Staging state to record the backup and tombstone in.
    """
    destination_backup = create_backup(destination, options=backup_options)
    if destination_backup is not None:
        staging.backups.append(destination_backup)
    staging.destination_tombstone, staging.destination_renamed = (
        stage_destination_for_replace(destination)
    )


def _stage_source_into_temp(
    *,
    source: Path,
    destination: Path,
    staging: _MoveStaging,
) -> None:
    """Move the source into staging, falling back to copy across devices.

    Args:
        source: Source path to move.
        destination: Destination path, used by the recursion guard.
        staging: Staging state to record the rename outcome in.

    Raises:
        OSError: When the rename fails for a reason other than ``EXDEV``, or
            when the copy fallback would recurse into the source tree.
    """
    try:
        source.replace(staging.temp_path)
        staging.source_moved_to_temp = True
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        reject_recursive_directory_copy(
            source=source,
            destination=destination,
            operation=FileOperation.MOVE,
        )
        _txn.copy_path(source=source, destination=staging.temp_path)
        sync_path(staging.temp_path)


def _validate_move(
    *,
    source: Path,
    destination: Path,
) -> None:
    """Validate a move request before touching the filesystem.

    Args:
        source: Source path to move.
        destination: Destination path to create or replace.

    Raises:
        FileNotFoundError: When the source or destination parent is missing.
    """
    validate_source(source=source, operation=FileOperation.MOVE)
    validate_destination_parent(destination)
