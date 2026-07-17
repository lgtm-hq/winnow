"""Transactional atomic filesystem operations."""

from __future__ import annotations

import errno
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from uuid import uuid4

from winnow.fs._path_ops import (
    copy_path,
    path_exists,
    remove_path,
    sync_directory,
    sync_path,
)
from winnow.fs.backup import create_backup
from winnow.fs.backup_options import BackupOptions
from winnow.fs.errors import FileSystemOperationError, FileSystemRollbackError
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation, OperationStatus

_LOGGER = logging.getLogger(__name__)

RollbackStep = Callable[[], None]
CommitStep = Callable[[], None]


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
        self._backup_options = _coerce_backup_options(backup)
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
        source = Path(source)
        destination = Path(destination)
        temp_path = _temporary_path(destination)
        destination_tombstone: Path | None = None
        destination_renamed = False
        backups: list[Path] = []

        try:
            _validate_source(source=source, operation=FileOperation.COPY)
            _validate_destination_parent(destination)
            copy_path(source=source, destination=temp_path)
            sync_path(temp_path)

            if path_exists(destination):
                backup_path = create_backup(
                    destination,
                    options=self._backup_options,
                )
                if backup_path is not None:
                    backups.append(backup_path)
                destination_tombstone, destination_renamed = (
                    _stage_destination_for_replace(destination)
                )

            temp_path.replace(destination)
            sync_directory(destination.parent)
        except (OSError, shutil.Error) as error:
            operation_error = _operation_error(
                operation=FileOperation.COPY,
                path=source,
                error=error,
            )
            cleanups: list[RollbackStep] = [lambda: _cleanup_path(temp_path)]
            if destination_tombstone is not None:
                tombstone = destination_tombstone
                if destination_renamed:
                    cleanups.append(
                        lambda: _restore_tombstone(
                            tombstone=tombstone,
                            destination=destination,
                        )
                    )
                else:
                    cleanups.append(lambda: _cleanup_path(tombstone))
            cleanups.append(lambda: _cleanup_backups(backups))
            _run_cleanups(operation_error, cleanups)
            raise operation_error from error

        log = OperationLog(
            operation=FileOperation.COPY,
            source=source,
            destination=destination,
            backups=tuple(backups),
            created_paths=(destination,),
        )
        self._record(
            log=log,
            rollback=lambda: _rollback_copy(
                destination=destination,
                destination_tombstone=destination_tombstone,
            ),
            commit=lambda: _commit_tombstones(destination_tombstone),
        )
        return log

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
        path = Path(path)
        tombstone = _temporary_path(path)
        backups: list[Path] = []

        try:
            _validate_source(source=path, operation=FileOperation.DELETE)
            backup_path = create_backup(path, options=self._backup_options)
            if backup_path is not None:
                backups.append(backup_path)
            path.replace(tombstone)
        except (OSError, shutil.Error) as error:
            operation_error = _operation_error(
                operation=FileOperation.DELETE,
                path=path,
                error=error,
            )
            _run_cleanups(
                operation_error,
                [
                    lambda: _cleanup_path(tombstone),
                    lambda: _cleanup_backups(backups),
                ],
            )
            raise operation_error from error

        log = OperationLog(
            operation=FileOperation.DELETE,
            source=path,
            backups=tuple(backups),
        )
        self._record(
            log=log,
            rollback=lambda: _restore_tombstone(
                tombstone=tombstone,
                destination=path,
            ),
            commit=lambda: _commit_tombstones(tombstone),
        )
        return log

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
        path = Path(path)
        created_paths: list[Path] = []

        try:
            if path_exists(path):
                if exist_ok and path.is_dir():
                    log = OperationLog(
                        operation=FileOperation.MKDIR,
                        destination=path,
                    )
                    self._record(log=log, rollback=lambda: None, commit=lambda: None)
                    return log
                raise FileExistsError(path)
            created_paths = _create_directories(path=path, parents=parents)
        except OSError as error:
            operation_error = _operation_error(
                operation=FileOperation.MKDIR,
                path=path,
                error=error,
            )
            _run_cleanups(
                operation_error,
                [lambda: _rollback_mkdir(created_paths)],
            )
            raise operation_error from error

        log = OperationLog(
            operation=FileOperation.MKDIR,
            destination=path,
            created_paths=tuple(created_paths),
        )
        self._record(
            log=log,
            rollback=lambda: _rollback_mkdir(created_paths),
            commit=lambda: None,
        )
        return log

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
        source = Path(source)
        destination = Path(destination)
        temp_path = _temporary_path(destination)
        destination_tombstone: Path | None = None
        destination_renamed = False
        destination_replaced = False
        source_tombstone: Path | None = None
        source_moved_to_temp = False
        backups: list[Path] = []

        try:
            _validate_source(source=source, operation=FileOperation.MOVE)
            _validate_destination_parent(destination)

            if path_exists(destination):
                destination_backup = create_backup(
                    destination,
                    options=self._backup_options,
                )
                if destination_backup is not None:
                    backups.append(destination_backup)
                destination_tombstone, destination_renamed = (
                    _stage_destination_for_replace(destination)
                )

            try:
                source.replace(temp_path)
                source_moved_to_temp = True
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                copy_path(source=source, destination=temp_path)
                sync_path(temp_path)

            temp_path.replace(destination)
            destination_replaced = True
            sync_directory(destination.parent)

            if not source_moved_to_temp:
                source_tombstone = _temporary_path(source)
                source.replace(source_tombstone)
        except (OSError, shutil.Error) as error:
            operation_error = _operation_error(
                operation=FileOperation.MOVE,
                path=source,
                error=error,
            )
            _run_cleanups(
                operation_error,
                [
                    lambda: _rollback_move(
                        source=source,
                        destination=destination,
                        temp_path=temp_path,
                        destination_tombstone=destination_tombstone,
                        source_tombstone=source_tombstone,
                        source_moved_to_temp=source_moved_to_temp,
                        destination_renamed=destination_renamed,
                        destination_replaced=destination_replaced,
                    ),
                    lambda: _cleanup_backups(backups),
                ],
            )
            raise operation_error from error

        log = OperationLog(
            operation=FileOperation.MOVE,
            source=source,
            destination=destination,
            backups=tuple(backups),
            created_paths=(destination,),
        )
        self._record(
            log=log,
            rollback=lambda: _rollback_move(
                source=source,
                destination=destination,
                temp_path=temp_path,
                destination_tombstone=destination_tombstone,
                source_tombstone=source_tombstone,
                source_moved_to_temp=source_moved_to_temp,
                destination_renamed=destination_renamed,
                destination_replaced=destination_replaced,
            ),
            commit=lambda: _commit_tombstones(
                destination_tombstone,
                source_tombstone,
            ),
        )
        return log

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

    def _record(
        self,
        *,
        log: OperationLog,
        rollback: RollbackStep,
        commit: CommitStep,
    ) -> None:
        """Record a completed operation and its lifecycle hooks."""
        self._logs.append(log)
        self._rollback_steps.append(rollback)
        self._commit_steps.append(commit)

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


def _cleanup_empty_directory(path: Path) -> None:
    """Remove an empty directory, ignoring paths already removed."""
    try:
        path.rmdir()
    except FileNotFoundError:
        return


def _cleanup_backups(backups: list[Path]) -> None:
    """Remove persistent backups orphaned by a failed operation."""
    errors: list[Exception] = []
    for backup in backups:
        try:
            _cleanup_path(backup)
        except (OSError, shutil.Error) as cleanup_error:
            errors.append(cleanup_error)
    if not errors:
        return
    aggregate_error = FileSystemOperationError(
        "failed to clean up filesystem backups",
        operation="fs.backup.cleanup",
        details={"errors": [str(error) for error in errors]},
    )
    for secondary_error in errors[1:]:
        aggregate_error.add_note(f"backup cleanup failed: {secondary_error}")
    raise aggregate_error from errors[0]


def _cleanup_path(path: Path) -> None:
    """Remove a path when it exists."""
    if path_exists(path):
        remove_path(path)


def _coerce_backup_options(backup: bool | BackupOptions) -> BackupOptions:
    """Normalize backup configuration input."""
    if isinstance(backup, BackupOptions):
        return backup
    return BackupOptions(enabled=backup)


def _commit_tombstones(*tombstones: Path | None) -> None:
    """Delete staging tombstones after a successful commit."""
    for tombstone in tombstones:
        if tombstone is not None and path_exists(tombstone):
            remove_path(tombstone)


def _create_directories(
    *,
    path: Path,
    parents: bool,
) -> list[Path]:
    """Create a directory path and return directories created."""
    if not parents:
        path.mkdir()
        return [path]

    missing_paths = _missing_directories(path)
    created_paths: list[Path] = []
    for missing_path in missing_paths:
        missing_path.mkdir()
        created_paths.append(missing_path)
    return created_paths


def _missing_directories(path: Path) -> list[Path]:
    """Return missing directories from highest parent to leaf."""
    missing_paths: list[Path] = []
    cursor = path
    while not path_exists(cursor):
        missing_paths.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    missing_paths.reverse()
    return missing_paths


def _operation_error(
    *,
    operation: FileOperation,
    path: Path,
    error: OSError | shutil.Error,
) -> FileSystemOperationError:
    """Build a structured filesystem operation error."""
    return FileSystemOperationError(
        f"failed to apply filesystem {operation.value}",
        operation=f"fs.{operation.value}",
        file_path=path,
        details={"error": str(error)},
    )


def _run_cleanups(
    error: FileSystemOperationError,
    cleanups: list[RollbackStep],
) -> None:
    """Run cleanup callables without masking the original operation error.

    Each callable is executed defensively so a cleanup failure never replaces the
    error that triggered the handler. Any cleanup failure is attached to ``error``
    as a note for diagnostics.

    Args:
        error: Operation error to annotate with cleanup failures.
        cleanups: Cleanup callables to execute in order.
    """
    for cleanup in cleanups:
        try:
            cleanup()
        except (OSError, shutil.Error, FileSystemOperationError) as cleanup_error:
            error.add_note(f"cleanup failed: {cleanup_error}")
            for note in getattr(cleanup_error, "__notes__", ()):
                error.add_note(f"cleanup detail: {note}")


def _restore_tombstone(
    *,
    tombstone: Path,
    destination: Path,
) -> None:
    """Move a staged tombstone back to its destination."""
    if not path_exists(tombstone):
        return
    if path_exists(destination):
        remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tombstone.replace(destination)


def _rollback_copy(
    *,
    destination: Path,
    destination_tombstone: Path | None,
) -> None:
    """Roll back a completed copy operation."""
    _cleanup_path(destination)
    if destination_tombstone is not None:
        _restore_tombstone(
            tombstone=destination_tombstone,
            destination=destination,
        )


def _rollback_mkdir(created_paths: list[Path]) -> None:
    """Roll back directories created by a mkdir operation."""
    for created_path in reversed(created_paths):
        _cleanup_empty_directory(created_path)


def _rollback_move(
    *,
    source: Path,
    destination: Path,
    temp_path: Path,
    destination_tombstone: Path | None,
    source_tombstone: Path | None,
    source_moved_to_temp: bool,
    destination_renamed: bool,
    destination_replaced: bool,
) -> None:
    """Roll back a completed or partially completed move operation."""
    if source_moved_to_temp:
        if (
            destination_replaced
            and path_exists(destination)
            and not path_exists(source)
        ):
            destination.replace(source)
        elif path_exists(temp_path) and not path_exists(source):
            temp_path.replace(source)
        # When the source exists again despite the move, leave the moved data
        # in place rather than discarding it.
    else:
        if destination_replaced:
            _cleanup_path(destination)
        _cleanup_path(temp_path)
        if source_tombstone is not None:
            _restore_tombstone(tombstone=source_tombstone, destination=source)

    if destination_tombstone is None:
        return
    if destination_renamed or destination_replaced:
        _restore_tombstone(
            tombstone=destination_tombstone,
            destination=destination,
        )
    else:
        # The destination was preserved in place (copy staging) and never
        # replaced, so only the staged copy needs to be discarded.
        _cleanup_path(destination_tombstone)


def _stage_destination_for_replace(destination: Path) -> tuple[Path, bool]:
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
    tombstone = _temporary_path(destination)
    if destination.is_dir() and not destination.is_symlink():
        destination.replace(tombstone)
        return tombstone, True
    try:
        copy_path(source=destination, destination=tombstone)
    except (OSError, shutil.Error) as error:
        try:
            _cleanup_path(tombstone)
        except (OSError, shutil.Error) as cleanup_error:
            error.add_note(f"cleanup failed: {cleanup_error}")
        raise
    return tombstone, False


def _temporary_path(path: Path) -> Path:
    """Return a unique staging path next to a destination path."""
    while True:
        candidate = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        if not path_exists(candidate):
            return candidate


def _validate_destination_parent(destination: Path) -> None:
    """Ensure a destination parent directory exists."""
    if not destination.parent.is_dir():
        raise FileNotFoundError(destination.parent)


def _validate_source(
    *,
    source: Path,
    operation: FileOperation,
) -> None:
    """Ensure a source path exists before applying an operation."""
    if not path_exists(source):
        raise FileNotFoundError(f"{operation.value} source not found: {source}")
