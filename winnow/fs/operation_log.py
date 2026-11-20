"""Structured log entries for filesystem operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from winnow.fs.operations import FileOperation, OperationStatus


@dataclass(slots=True)
class OperationLog:
    """Record of an applied filesystem operation.

    Args:
        operation: Operation type that was applied.
        source: Source path used by the operation, when applicable.
        destination: Destination path used by the operation, when applicable.
        backups: Persistent backup paths created for the operation.
        created_paths: Filesystem paths created or staged by the operation.
        status: Current lifecycle state of the operation.
    """

    operation: FileOperation
    source: Path | None = None
    destination: Path | None = None
    backups: tuple[Path, ...] = field(default_factory=tuple)
    created_paths: tuple[Path, ...] = field(default_factory=tuple)
    status: OperationStatus = OperationStatus.APPLIED

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation of the operation log.

        Returns:
            Operation metadata with paths represented as strings.
        """
        result: dict[str, object] = {
            "operation": self.operation.value,
            "status": self.status.value,
        }
        if self.source is not None:
            result["source"] = str(self.source)
        if self.destination is not None:
            result["destination"] = str(self.destination)
        if self.backups:
            result["backups"] = [str(path) for path in self.backups]
        if self.created_paths:
            result["created_paths"] = [str(path) for path in self.created_paths]
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        """Rebuild an operation log from its :meth:`as_dict` representation.

        Args:
            data: Mapping produced by :meth:`as_dict`. Optional keys fall back
                to the dataclass defaults.

        Returns:
            An operation log equal to the one that produced ``data``.

        Raises:
            ValueError: When ``operation`` is missing, either enum value is
                unknown, ``source``/``destination`` is present but not a
                string, or ``backups``/``created_paths`` is present but not a
                list of strings.
        """
        operation = data.get("operation")
        if operation is None:
            raise ValueError("operation log data is missing 'operation'")
        return cls(
            operation=FileOperation(str(operation)),
            source=_path_from(data.get("source"), key="source"),
            destination=_path_from(data.get("destination"), key="destination"),
            backups=_paths_from(data.get("backups")),
            created_paths=_paths_from(data.get("created_paths")),
            status=OperationStatus(
                str(data.get("status", OperationStatus.APPLIED.value)),
            ),
        )


def _path_from(value: object, *, key: str) -> Path | None:
    """Decode one optional serialized path.

    Args:
        value: JSON string, or ``None`` when absent.
        key: Field name used in the error message.

    Returns:
        The path, or ``None`` when ``value`` is ``None``.

    Raises:
        ValueError: When ``value`` is present but not a string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"operation log '{key}' must be a string")
    return Path(value)


def _paths_from(value: object) -> tuple[Path, ...]:
    """Decode a serialized path list.

    Args:
        value: JSON list of path strings, or ``None`` when absent.

    Returns:
        The paths as a tuple; empty when ``value`` is ``None``.

    Raises:
        ValueError: When ``value`` is present but not a list of strings.
    """
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError("operation log path list must be a list of strings")
    return tuple(Path(item) for item in value)
