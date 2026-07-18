"""Delete operation: forward apply and tombstone handling.

Patchable filesystem helpers (``_cleanup_path``, ``_restore_tombstone``, ...)
are resolved through :mod:`winnow.fs.transaction` at call time so
failure-injection tests keep a single stable patch point.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Circular by design: winnow.fs.transaction imports this module and hosts the
# patchable helper bindings. Attributes are only resolved at call time, after
# both modules have fully initialized.
from winnow.fs import transaction as _txn
from winnow.fs._operation import (
    AppliedOperation,
    operation_error,
    validate_source,
)
from winnow.fs._path_ops import temporary_path
from winnow.fs.backup import create_backup
from winnow.fs.backup_options import BackupOptions
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation


def apply_delete(
    *,
    path: Path,
    backup_options: BackupOptions,
) -> AppliedOperation:
    """Atomically delete a path using a staged tombstone.

    Args:
        path: Existing file, directory, or symlink to delete.
        backup_options: Backup configuration for the deleted path.

    Returns:
        Applied operation with its log entry and lifecycle hooks.

    Raises:
        FileSystemOperationError: When the path cannot be deleted.
    """
    tombstone = temporary_path(path)
    backups: list[Path] = []

    try:
        validate_source(source=path, operation=FileOperation.DELETE)
        backup_path = create_backup(path, options=backup_options)
        if backup_path is not None:
            backups.append(backup_path)
        path.replace(tombstone)
    except (OSError, shutil.Error) as error:
        failure = operation_error(
            operation=FileOperation.DELETE,
            path=path,
            error=error,
        )
        _txn._run_cleanups(
            failure,
            [
                lambda: _txn._cleanup_path(tombstone),
                lambda: _txn._cleanup_backups(backups),
            ],
        )
        raise failure from error

    log = OperationLog(
        operation=FileOperation.DELETE,
        source=path,
        backups=tuple(backups),
    )
    return AppliedOperation(
        log=log,
        rollback=lambda: _txn._restore_tombstone(
            tombstone=tombstone,
            destination=path,
        ),
        commit=lambda: _txn._commit_tombstones(tombstone),
    )
