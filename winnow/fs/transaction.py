"""Transactional atomic filesystem operations.

Each operation's forward and rollback logic lives in its own internal module
(:mod:`winnow.fs._copy`, :mod:`winnow.fs._move`, :mod:`winnow.fs._delete`,
:mod:`winnow.fs._mkdir`). This module keeps the thin
:class:`FileSystemTransaction` orchestrator, the public ``atomic_*`` wrappers,
and the patchable helper bindings the operation modules resolve at call time,
so failure-injection tests keep a single stable patch point.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from winnow.fs import _cleanup, _path_ops
from winnow.fs._operation import (
    AppliedOperation,
    CommitStep,
    RollbackStep,
)
from winnow.fs.backup_options import BackupOptions, coerce_backup_options
from winnow.fs.errors import FileSystemOperationError, FileSystemRollbackError
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import OperationStatus

_LOGGER = logging.getLogger(__name__)

# Shared filesystem helpers bound as module-level definitions so this entry
# point keeps independently patchable bindings for failure injection. The
# per-operation modules resolve these names through this module at call time.
copy_path = _path_ops.copy_path
_cleanup_path = _cleanup.cleanup_path
_commit_tombstones = _cleanup.commit_tombstones
_missing_directories = _cleanup.missing_directories
_restore_tombstone = _cleanup.restore_tombstone
_run_cleanups = _cleanup.run_cleanups

# Imported after the bindings above so the operation modules can safely bind
# this partially initialized module during the import cycle.
from winnow.fs import _copy, _delete, _mkdir, _move  # noqa: E402


class FileSystemTransaction:
    """Context manager for batched atomic filesystem operations.

    Operations applied inside the context are committed when the context exits
    successfully. If any exception escapes the context, completed operations are
    rolled back in reverse order.

    Args:
        backup: Backup configuration for destructive operations. Passing ``False``
            disables user-visible backups while preserving rollback staging.
    """

    def __init__(
        self,
        *,
        backup: bool | BackupOptions = True,
    ) -> None:
        self._backup_options = coerce_backup_options(backup)
        self._rollback_steps: list[RollbackStep] = []
        self._commit_steps: list[CommitStep] = []
        self._logs: list[OperationLog] = []
        self._active = False
        self._closed = False
        self.rollback_errors: tuple[Exception, ...] = ()

    @property
    def logs(self) -> tuple[OperationLog, ...]:
        """Return immutable operation log entries for this transaction."""
        return tuple(self._logs)

    def __enter__(self) -> Self:
        """Enter the transactional context.

        Returns:
            This transaction instance.

        Raises:
            FileSystemOperationError: When the transaction is already active,
                which would indicate an unsupported nested entry.
        """
        if self._active:
            raise FileSystemOperationError(
                "cannot enter an already active filesystem transaction",
                operation="fs.transaction.enter",
            )
        self._logs.clear()
        self._rollback_steps.clear()
        self._commit_steps.clear()
        self.rollback_errors = ()
        self._active = True
        self._closed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Commit or roll back the transaction when leaving the context.

        Args:
            exc_type: Escaping exception type, if any.
            exc_value: Escaping exception value, if any.
            traceback: Escaping exception traceback, if any.

        Returns:
            ``False`` so exceptions from the managed block are never suppressed.
        """
        del traceback
        if exc_type is None:
            self.commit()
            return False
        self.rollback_errors = self.rollback()
        if self.rollback_errors:
            _LOGGER.warning(
                "filesystem transaction rollback left %d step(s) unrestored",
                len(self.rollback_errors),
            )
            if exc_value is not None:
                for rollback_error in self.rollback_errors:
                    exc_value.add_note(f"rollback failed: {rollback_error}")
        return False

    def copy(
        self,
        source: Path,
        destination: Path,
    ) -> OperationLog:
        """Atomically copy a path into place.

        Args:
            source: Existing file, directory, or symlink to copy.
            destination: Destination path to create or replace.

        Returns:
            Structured log entry for the applied operation.

        Raises:
            FileSystemOperationError: When the copy cannot be applied.
        """
        self._require_active()
        return self._record(
            _copy.apply_copy(
                source=Path(source),
                destination=Path(destination),
                backup_options=self._backup_options,
            ),
        )

    def delete(
        self,
        path: Path,
    ) -> OperationLog:
        """Atomically delete a path using a staged tombstone.

        Args:
            path: Existing file, directory, or symlink to delete.

        Returns:
            Structured log entry for the applied operation.

        Raises:
            FileSystemOperationError: When the path cannot be deleted.
        """
        self._require_active()
        return self._record(
            _delete.apply_delete(
                path=Path(path),
                backup_options=self._backup_options,
            ),
        )

    def mkdir(
        self,
        path: Path,
        *,
        parents: bool = True,
        exist_ok: bool = False,
    ) -> OperationLog:
        """Create a directory and register rollback for created directories.

        Args:
            path: Directory path to create.
            parents: Whether missing parent directories should be created.
            exist_ok: Whether an existing directory should be accepted.

        Returns:
            Structured log entry for the applied operation.

        Raises:
            FileSystemOperationError: When the directory cannot be created.
        """
        self._require_active()
        return self._record(
            _mkdir.apply_mkdir(
                path=Path(path),
                parents=parents,
                exist_ok=exist_ok,
            ),
        )

    def move(
        self,
        source: Path,
        destination: Path,
    ) -> OperationLog:
        """Atomically move a path into place.

        Only an overwritten destination is backed up: a plain rename destroys
        nothing (rollback restores it by renaming back), so backing up the
        source would double I/O for every move.

        Args:
            source: Existing file, directory, or symlink to move.
            destination: Destination path to create or replace.

        Returns:
            Structured log entry for the applied operation.

        Raises:
            FileSystemOperationError: When the move cannot be applied.
        """
        self._require_active()
        return self._record(
            _move.apply_move(
                source=Path(source),
                destination=Path(destination),
                backup_options=self._backup_options,
            ),
        )

    def commit(self) -> None:
        """Commit applied operations and clean up staging paths.

        Every commit cleanup is attempted even when earlier ones fail, so a
        single failing callback cannot strand tombstones that later callbacks
        would remove.

        Raises:
            FileSystemOperationError: When commit cleanup fails.
        """
        if self._closed:
            return
        self._closed = True
        self._active = False
        errors: list[Exception] = []
        for step in self._commit_steps:
            try:
                step()
            except (OSError, shutil.Error) as error:
                errors.append(error)
        self._rollback_steps.clear()
        self._commit_steps.clear()
        if errors:
            raise FileSystemOperationError(
                "failed to commit filesystem transaction",
                operation="fs.transaction.commit",
                details={"errors": [str(error) for error in errors]},
            ) from errors[0]

    def rollback(self) -> tuple[Exception, ...]:
        """Roll back applied operations in reverse order.

        Returns:
            Rollback errors encountered while attempting to restore prior state.
        """
        if self._closed:
            return self.rollback_errors
        errors: list[Exception] = []
        for log, step in zip(
            reversed(self._logs),
            reversed(self._rollback_steps),
            strict=True,
        ):
            try:
                step()
            except (OSError, shutil.Error) as error:
                errors.append(error)
            else:
                log.status = OperationStatus.ROLLED_BACK
        self._rollback_steps.clear()
        self._commit_steps.clear()
        self._closed = True
        self._active = False
        self.rollback_errors = tuple(errors)
        return self.rollback_errors

    def rollback_or_raise(self) -> None:
        """Roll back applied operations and raise when rollback is incomplete.

        Raises:
            FileSystemRollbackError: When any rollback step fails.
        """
        errors = self.rollback()
        if errors:
            raise FileSystemRollbackError(
                "failed to fully roll back filesystem transaction",
                operation="fs.transaction.rollback",
                details={"errors": [str(error) for error in errors]},
            ) from errors[0]

    def _record(self, applied: AppliedOperation) -> OperationLog:
        """Record an applied operation's lifecycle hooks and return its log.

        Args:
            applied: Applied operation with its lifecycle hooks.

        Returns:
            Structured log entry for the applied operation.
        """
        self._logs.append(applied.log)
        self._rollback_steps.append(applied.rollback)
        self._commit_steps.append(applied.commit)
        return applied.log

    def _require_active(self) -> None:
        """Ensure the transaction is active before mutating the filesystem.

        Raises:
            FileSystemOperationError: When the transaction has not been entered
                or has already been committed or rolled back.
        """
        if not self._active:
            raise FileSystemOperationError(
                "filesystem transaction is not active",
                operation="fs.transaction",
            )


def atomic_copy(
    source: Path,
    destination: Path,
    *,
    backup: bool | BackupOptions = True,
) -> OperationLog:
    """Atomically copy a path outside an explicit transaction.

    Args:
        source: Existing file, directory, or symlink to copy.
        destination: Destination path to create or replace.
        backup: Backup configuration for replaced destinations.

    Returns:
        Structured log entry for the copy operation.
    """
    with FileSystemTransaction(backup=backup) as transaction:
        return transaction.copy(source=source, destination=destination)


def atomic_delete(
    path: Path,
    *,
    backup: bool | BackupOptions = True,
) -> OperationLog:
    """Atomically delete a path outside an explicit transaction.

    Args:
        path: Existing file, directory, or symlink to delete.
        backup: Backup configuration for the deleted path.

    Returns:
        Structured log entry for the delete operation.
    """
    with FileSystemTransaction(backup=backup) as transaction:
        return transaction.delete(path)


def atomic_mkdir(
    path: Path,
    *,
    parents: bool = True,
    exist_ok: bool = False,
    backup: bool | BackupOptions = True,
) -> OperationLog:
    """Atomically create a directory outside an explicit transaction.

    Args:
        path: Directory path to create.
        parents: Whether missing parent directories should be created.
        exist_ok: Whether an existing directory should be accepted.
        backup: Accepted for API consistency with other atomic helpers.

    Returns:
        Structured log entry for the mkdir operation.
    """
    with FileSystemTransaction(backup=backup) as transaction:
        return transaction.mkdir(path=path, parents=parents, exist_ok=exist_ok)


def atomic_move(
    source: Path,
    destination: Path,
    *,
    backup: bool | BackupOptions = True,
) -> OperationLog:
    """Atomically move a path outside an explicit transaction.

    Args:
        source: Existing file, directory, or symlink to move.
        destination: Destination path to create or replace.
        backup: Backup configuration for replaced destinations.

    Returns:
        Structured log entry for the move operation.
    """
    with FileSystemTransaction(backup=backup) as transaction:
        return transaction.move(source=source, destination=destination)


def transactional_file_ops(
    *,
    backup: bool | BackupOptions = True,
) -> FileSystemTransaction:
    """Create a filesystem transaction context manager.

    Args:
        backup: Backup configuration for destructive operations.

    Returns:
        Transaction object that can be used as a context manager.
    """
    return FileSystemTransaction(backup=backup)


def _cleanup_backups(backups: list[Path]) -> None:
    """Remove persistent backups orphaned by a failed operation.

    Args:
        backups: Backup paths created before the operation failed.

    Raises:
        FileSystemOperationError: When any backup cannot be removed. Every
            backup is attempted before the aggregate error is raised.
    """
    _cleanup.cleanup_backups(backups, cleanup=_cleanup_path)
