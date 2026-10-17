"""Reversible filesystem commands for the winnow pipeline.

Each command wraps the atomic helpers in :mod:`winnow.fs` so file mutations stay
crash-safe and reversible. Commands implement the command pattern: :meth:`execute`
applies a change and records enough state to reverse it, while :meth:`undo`
restores the prior filesystem state. Commands serialize to and from plain dicts so
a saga can persist them in a transaction log and reconstruct them later.

The base :class:`Command` owns the shared lifecycle (re-execution guards, error
wrapping, log bookkeeping) and derives serialization from dataclass fields, so
each concrete command only supplies its filesystem call and its reversal.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import ClassVar

from winnow.exceptions import PipelineError
from winnow.fs import (
    BackupOptions,
    OperationLog,
    atomic_copy,
    atomic_delete,
    atomic_mkdir,
    atomic_move,
    restore_backup,
)
from winnow.fs.errors import FileSystemOperationError

_FS_ERRORS = (FileSystemOperationError, OSError, shutil.Error)


@dataclass(slots=True)
class Command(ABC):
    """Abstract reversible filesystem command.

    Concrete commands mutate the filesystem through :mod:`winnow.fs` atomic
    helpers. A failed :meth:`execute` leaves the filesystem unchanged because the
    underlying helpers roll back partial work. A successful :meth:`execute` records
    the state required for :meth:`undo` to restore the prior filesystem contents.

    Subclasses implement :meth:`_apply` and :meth:`_revert`; the base class
    handles execution-state tracking, error wrapping, and dict serialization
    derived from the subclass's dataclass fields.
    """

    command_name: ClassVar[str]
    _execute_path_field: ClassVar[str] = "path"
    _undo_path_field: ClassVar[str] = "path"

    _log: OperationLog | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def execute(self) -> OperationLog:
        """Apply the command's filesystem mutation.

        Returns:
            Structured log entry describing the applied operation.

        Raises:
            PipelineError: When the mutation cannot be applied or has already run.
        """
        if self._log is not None:
            raise PipelineError(
                "command has already been executed",
                operation=f"pipeline.{self.command_name}.execute",
            )
        try:
            log = self._apply()
        except _FS_ERRORS as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=getattr(self, self._execute_path_field),
            ) from error
        self._log = log
        return log

    def undo(self) -> None:
        """Reverse a previously executed command.

        Raises:
            PipelineError: When the command has not run or cannot be reversed.
        """
        if self._log is None:
            raise PipelineError(
                "cannot undo a command that has not been executed",
                operation=f"pipeline.{self.command_name}.undo",
            )
        try:
            self._revert(self._log)
        except _FS_ERRORS as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=getattr(self, self._undo_path_field),
                undo=True,
            ) from error
        self._log = None

    def restore_log(self, log: OperationLog) -> None:
        """Attach the operation log of an execution performed by another instance.

        Lets a command rebuilt via :meth:`from_dict` be reversed with
        :meth:`undo` using a log persisted elsewhere (for example the saga
        session log).

        Args:
            log: Operation log recorded when the original instance executed.

        Raises:
            PipelineError: When the command already holds an operation log.
        """
        if self._log is not None:
            raise PipelineError(
                "command already has an operation log",
                operation=f"pipeline.{self.command_name}.restore_log",
            )
        self._log = log

    def to_dict(self) -> dict[str, object]:
        """Serialize the command to a JSON-friendly dict.

        Returns:
            Declarative command parameters keyed by ``command`` type name, with
            one entry per dataclass field in declaration order.
        """
        payload: dict[str, object] = {"command": self.command_name}
        for spec in fields(self):
            if not spec.init:
                continue
            value = getattr(self, spec.name)
            if isinstance(value, Path):
                payload[spec.name] = str(value)
            elif isinstance(value, BackupOptions):
                payload[spec.name] = _backup_to_dict(value)
            else:
                payload[spec.name] = value
        return payload

    @staticmethod
    def from_dict(data: dict[str, object]) -> Command:
        """Reconstruct a command from its serialized representation.

        Args:
            data: Mapping produced by :meth:`to_dict`.

        Returns:
            A fresh, unexecuted command instance.

        Raises:
            PipelineError: When the command type is missing or unknown.
        """
        name = data.get("command")
        if not isinstance(name, str):
            raise PipelineError(
                "serialized command is missing a 'command' type",
                operation="pipeline.command.from_dict",
                details={"data": dict(data)},
            )
        command_type = _COMMAND_REGISTRY.get(name)
        if command_type is None:
            raise PipelineError(
                "unknown pipeline command type",
                operation="pipeline.command.from_dict",
                details={"command": name},
            )
        return command_type._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, object]) -> Command:
        """Build a concrete command from its serialized fields.

        Field values are decoded by annotation: ``Path`` fields are required
        strings, ``backup`` fields accept bools or backup-option dicts, and
        remaining fields fall back to their dataclass defaults.

        Raises:
            PipelineError: When a required path field is missing.
        """
        kwargs: dict[str, object] = {}
        for spec in fields(cls):
            if not spec.init:
                continue
            default = None if spec.default is MISSING else spec.default
            if spec.type == "Path":
                kwargs[spec.name] = Path(_require_str(data, spec.name))
            elif spec.type == "bool | BackupOptions":
                kwargs[spec.name] = _backup_from_dict(data.get(spec.name, default))
            else:
                kwargs[spec.name] = bool(data.get(spec.name, default))
        return cls(**kwargs)

    @abstractmethod
    def _apply(self) -> OperationLog:
        """Perform the filesystem mutation and return its operation log."""

    @abstractmethod
    def _revert(self, log: OperationLog) -> None:
        """Reverse the filesystem mutation recorded in ``log``."""


@dataclass(slots=True)
class _TransferCommand(Command):
    """Base for commands that transfer content from a source to a destination.

    Args:
        source: Existing path to transfer.
        destination: Destination path to create or replace.
        backup: Backup configuration used when overwriting the destination.
    """

    _execute_path_field: ClassVar[str] = "source"
    _undo_path_field: ClassVar[str] = "destination"

    source: Path
    destination: Path
    backup: bool | BackupOptions = True


@dataclass(slots=True)
class MoveFile(_TransferCommand):
    """Move a file, directory, or symlink to a new location.

    Args:
        source: Existing path to move.
        destination: Destination path to create or replace.
        backup: Backup configuration used when overwriting the destination.
    """

    command_name: ClassVar[str] = "move_file"

    def _apply(self) -> OperationLog:
        """Move ``source`` onto ``destination``."""
        return atomic_move(
            source=self.source,
            destination=self.destination,
            backup=self.backup,
        )

    def _revert(self, log: OperationLog) -> None:
        """Move ``destination`` back to ``source`` and restore any overwrite."""
        atomic_move(
            source=self.destination,
            destination=self.source,
            backup=False,
        )
        if log.backups:
            restore_backup(
                backup_path=log.backups[0],
                destination=self.destination,
            )


@dataclass(slots=True)
class CopyFile(_TransferCommand):
    """Copy a file, directory, or symlink to a new location.

    Args:
        source: Existing path to copy.
        destination: Destination path to create or replace.
        backup: Backup configuration used when overwriting the destination.
    """

    command_name: ClassVar[str] = "copy_file"

    def _apply(self) -> OperationLog:
        """Copy ``source`` onto ``destination``."""
        return atomic_copy(
            source=self.source,
            destination=self.destination,
            backup=self.backup,
        )

    def _revert(self, log: OperationLog) -> None:
        """Remove the copied destination and restore any overwritten content."""
        if log.backups:
            restore_backup(
                backup_path=log.backups[0],
                destination=self.destination,
            )
        else:
            atomic_delete(self.destination, backup=False)


@dataclass(slots=True)
class DeleteFile(Command):
    """Delete a file, directory, or symlink, retaining a backup for undo.

    Args:
        path: Existing path to delete.
        backup: Backup configuration. A backup is required for :meth:`undo`.
    """

    command_name: ClassVar[str] = "delete_file"

    path: Path
    backup: bool | BackupOptions = True

    def _apply(self) -> OperationLog:
        """Delete ``path`` after staging a backup."""
        return atomic_delete(self.path, backup=self.backup)

    def _revert(self, log: OperationLog) -> None:
        """Restore the deleted path from its backup.

        Raises:
            PipelineError: When no backup was retained for the delete.
        """
        if not log.backups:
            raise PipelineError(
                "cannot undo delete without a retained backup",
                operation=f"pipeline.{self.command_name}.undo",
                file_path=self.path,
            )
        restore_backup(
            backup_path=log.backups[0],
            destination=self.path,
        )


@dataclass(slots=True)
class CreateDirectory(Command):
    """Create a directory, tracking created directories for undo.

    Args:
        path: Directory path to create.
        parents: Whether missing parent directories should be created.
        exist_ok: Whether an existing directory should be accepted.
    """

    command_name: ClassVar[str] = "create_directory"

    path: Path
    parents: bool = True
    exist_ok: bool = False

    def _apply(self) -> OperationLog:
        """Create ``path`` and record the directories that were created."""
        return atomic_mkdir(
            path=self.path,
            parents=self.parents,
            exist_ok=self.exist_ok,
        )

    def _revert(self, log: OperationLog) -> None:
        """Remove directories created by :meth:`execute`, leaf first.

        Raises:
            PipelineError: When a created directory is not empty and therefore
                cannot be safely removed.
        """
        for created in reversed(log.created_paths):
            if not created.is_dir():
                continue
            try:
                created.rmdir()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _wrap_error(
                    error,
                    command=self.command_name,
                    path=created,
                    undo=True,
                ) from error


def _backup_to_dict(backup: bool | BackupOptions) -> object:
    """Serialize a backup configuration to a JSON-friendly value."""
    if isinstance(backup, BackupOptions):
        return {
            "enabled": backup.enabled,
            "directory": (
                str(backup.directory) if backup.directory is not None else None
            ),
            "suffix": backup.suffix,
        }
    return backup


def _backup_from_dict(value: object) -> bool | BackupOptions:
    """Deserialize a backup configuration produced by :func:`_backup_to_dict`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        directory = value.get("directory")
        return BackupOptions(
            enabled=bool(value.get("enabled", True)),
            directory=Path(directory) if isinstance(directory, str) else None,
            suffix=str(value.get("suffix", ".bak")),
        )
    return True


def _require_str(data: dict[str, object], key: str) -> str:
    """Return a required string field from serialized command data.

    Raises:
        PipelineError: When the field is missing or not a string.
    """
    value = data.get(key)
    if not isinstance(value, str):
        raise PipelineError(
            f"serialized command is missing string field '{key}'",
            operation="pipeline.command.from_dict",
            details={"data": dict(data)},
        )
    return value


def _wrap_error(
    error: Exception,
    *,
    command: str,
    path: Path,
    undo: bool = False,
) -> PipelineError:
    """Wrap a low-level filesystem error as a :class:`PipelineError`."""
    phase = "undo" if undo else "execute"
    return PipelineError(
        f"pipeline command {command} failed during {phase}",
        operation=f"pipeline.{command}.{phase}",
        file_path=path,
        details={"error": str(error)},
    )


_COMMAND_REGISTRY: dict[str, type[Command]] = {
    command.command_name: command
    for command in (MoveFile, CopyFile, DeleteFile, CreateDirectory)
}


__all__ = [
    "Command",
    "CopyFile",
    "CreateDirectory",
    "DeleteFile",
    "MoveFile",
]
