"""Reversible filesystem commands for the winnow pipeline.

Each command wraps the atomic helpers in :mod:`winnow.fs` so file mutations stay
crash-safe and reversible. Commands implement the command pattern: :meth:`execute`
applies a change and records enough state to reverse it, while :meth:`undo`
restores the prior filesystem state. Commands serialize to and from plain dicts so
a saga can persist them in a transaction log and reconstruct them later.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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


class Command(ABC):
    """Abstract reversible filesystem command.

    Concrete commands mutate the filesystem through :mod:`winnow.fs` atomic
    helpers. A failed :meth:`execute` leaves the filesystem unchanged because the
    underlying helpers roll back partial work. A successful :meth:`execute` records
    the state required for :meth:`undo` to restore the prior filesystem contents.
    """

    __slots__ = ()

    command_name: ClassVar[str]

    @abstractmethod
    def execute(self) -> OperationLog:
        """Apply the command's filesystem mutation.

        Returns:
            Structured log entry describing the applied operation.

        Raises:
            PipelineError: When the mutation cannot be applied or has already run.
        """

    @abstractmethod
    def undo(self) -> None:
        """Reverse a previously executed command.

        Raises:
            PipelineError: When the command has not run or cannot be reversed.
        """

    @abstractmethod
    def to_dict(self) -> dict[str, object]:
        """Serialize the command to a JSON-friendly dict.

        Returns:
            Declarative command parameters keyed by ``command`` type name.
        """

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
    @abstractmethod
    def _from_dict(cls, data: dict[str, object]) -> Command:
        """Build a concrete command from its serialized fields."""


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


@dataclass(slots=True)
class MoveFile(Command):
    """Move a file, directory, or symlink to a new location.

    Args:
        source: Existing path to move.
        destination: Destination path to create or replace.
        backup: Backup configuration used when overwriting the destination.
    """

    command_name: ClassVar[str] = "move_file"

    source: Path
    destination: Path
    backup: bool | BackupOptions = True
    _log: OperationLog | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def execute(self) -> OperationLog:
        """Move ``source`` onto ``destination``.

        Returns:
            Structured log entry for the move.

        Raises:
            PipelineError: When the move fails or the command already ran.
        """
        _reject_reexecution(self._log, command=self.command_name)
        try:
            log = atomic_move(
                source=self.source,
                destination=self.destination,
                backup=self.backup,
            )
        except (FileSystemOperationError, OSError, shutil.Error) as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=self.source,
            ) from error
        self._log = log
        return log

    def undo(self) -> None:
        """Move ``destination`` back to ``source`` and restore any overwrite.

        Raises:
            PipelineError: When the command has not run or cannot be reversed.
        """
        log = _require_execution(self._log, command=self.command_name)
        try:
            atomic_move(
                source=self.destination,
                destination=self.source,
                backup=False,
            )
            _restore_first_backup(log, destination=self.destination)
        except (FileSystemOperationError, OSError, shutil.Error) as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=self.destination,
                undo=True,
            ) from error
        self._log = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the move command."""
        return {
            "command": self.command_name,
            "source": str(self.source),
            "destination": str(self.destination),
            "backup": _backup_to_dict(self.backup),
        }

    @classmethod
    def _from_dict(cls, data: dict[str, object]) -> MoveFile:
        """Rebuild a move command from serialized fields."""
        return cls(
            source=Path(_require_str(data, "source")),
            destination=Path(_require_str(data, "destination")),
            backup=_backup_from_dict(data.get("backup", True)),
        )


@dataclass(slots=True)
class CopyFile(Command):
    """Copy a file, directory, or symlink to a new location.

    Args:
        source: Existing path to copy.
        destination: Destination path to create or replace.
        backup: Backup configuration used when overwriting the destination.
    """

    command_name: ClassVar[str] = "copy_file"

    source: Path
    destination: Path
    backup: bool | BackupOptions = True
    _log: OperationLog | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def execute(self) -> OperationLog:
        """Copy ``source`` onto ``destination``.

        Returns:
            Structured log entry for the copy.

        Raises:
            PipelineError: When the copy fails or the command already ran.
        """
        _reject_reexecution(self._log, command=self.command_name)
        try:
            log = atomic_copy(
                source=self.source,
                destination=self.destination,
                backup=self.backup,
            )
        except (FileSystemOperationError, OSError, shutil.Error) as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=self.source,
            ) from error
        self._log = log
        return log

    def undo(self) -> None:
        """Remove the copied destination and restore any overwritten content.

        Raises:
            PipelineError: When the command has not run or cannot be reversed.
        """
        log = _require_execution(self._log, command=self.command_name)
        try:
            if log.backups:
                restore_backup(
                    backup_path=log.backups[0],
                    destination=self.destination,
                )
            else:
                atomic_delete(self.destination, backup=False)
        except (FileSystemOperationError, OSError, shutil.Error) as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=self.destination,
                undo=True,
            ) from error
        self._log = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the copy command."""
        return {
            "command": self.command_name,
            "source": str(self.source),
            "destination": str(self.destination),
            "backup": _backup_to_dict(self.backup),
        }

    @classmethod
    def _from_dict(cls, data: dict[str, object]) -> CopyFile:
        """Rebuild a copy command from serialized fields."""
        return cls(
            source=Path(_require_str(data, "source")),
            destination=Path(_require_str(data, "destination")),
            backup=_backup_from_dict(data.get("backup", True)),
        )


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
    _log: OperationLog | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def execute(self) -> OperationLog:
        """Delete ``path`` after staging a backup.

        Returns:
            Structured log entry for the delete.

        Raises:
            PipelineError: When the delete fails or the command already ran.
        """
        _reject_reexecution(self._log, command=self.command_name)
        try:
            log = atomic_delete(self.path, backup=self.backup)
        except (FileSystemOperationError, OSError, shutil.Error) as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=self.path,
            ) from error
        self._log = log
        return log

    def undo(self) -> None:
        """Restore the deleted path from its backup.

        Raises:
            PipelineError: When the command has not run or no backup exists.
        """
        log = _require_execution(self._log, command=self.command_name)
        if not log.backups:
            raise PipelineError(
                "cannot undo delete without a retained backup",
                operation=f"pipeline.{self.command_name}.undo",
                file_path=self.path,
            )
        try:
            restore_backup(
                backup_path=log.backups[0],
                destination=self.path,
            )
        except (FileSystemOperationError, OSError, shutil.Error) as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=self.path,
                undo=True,
            ) from error
        self._log = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the delete command."""
        return {
            "command": self.command_name,
            "path": str(self.path),
            "backup": _backup_to_dict(self.backup),
        }

    @classmethod
    def _from_dict(cls, data: dict[str, object]) -> DeleteFile:
        """Rebuild a delete command from serialized fields."""
        return cls(
            path=Path(_require_str(data, "path")),
            backup=_backup_from_dict(data.get("backup", True)),
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
    _log: OperationLog | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def execute(self) -> OperationLog:
        """Create ``path`` and record the directories that were created.

        Returns:
            Structured log entry for the mkdir.

        Raises:
            PipelineError: When the directory cannot be created or already ran.
        """
        _reject_reexecution(self._log, command=self.command_name)
        try:
            log = atomic_mkdir(
                path=self.path,
                parents=self.parents,
                exist_ok=self.exist_ok,
            )
        except (FileSystemOperationError, OSError, shutil.Error) as error:
            raise _wrap_error(
                error,
                command=self.command_name,
                path=self.path,
            ) from error
        self._log = log
        return log

    def undo(self) -> None:
        """Remove directories created by :meth:`execute`, leaf first.

        Raises:
            PipelineError: When the command has not run or a directory is not
                empty and therefore cannot be safely removed.
        """
        log = _require_execution(self._log, command=self.command_name)
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
        self._log = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the create-directory command."""
        return {
            "command": self.command_name,
            "path": str(self.path),
            "parents": self.parents,
            "exist_ok": self.exist_ok,
        }

    @classmethod
    def _from_dict(cls, data: dict[str, object]) -> CreateDirectory:
        """Rebuild a create-directory command from serialized fields."""
        return cls(
            path=Path(_require_str(data, "path")),
            parents=bool(data.get("parents", True)),
            exist_ok=bool(data.get("exist_ok", False)),
        )


def _reject_reexecution(log: OperationLog | None, *, command: str) -> None:
    """Reject executing a command that already applied an operation.

    Raises:
        PipelineError: When the command has already been executed.
    """
    if log is not None:
        raise PipelineError(
            "command has already been executed",
            operation=f"pipeline.{command}.execute",
        )


def _require_execution(log: OperationLog | None, *, command: str) -> OperationLog:
    """Return the operation log for an executed command.

    Raises:
        PipelineError: When the command has not been executed.
    """
    if log is None:
        raise PipelineError(
            "cannot undo a command that has not been executed",
            operation=f"pipeline.{command}.undo",
        )
    return log


def _restore_first_backup(log: OperationLog, *, destination: Path) -> None:
    """Restore the first recorded backup to a destination when present."""
    if log.backups:
        restore_backup(backup_path=log.backups[0], destination=destination)


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
