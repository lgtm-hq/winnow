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
            ValueError: When ``operation`` is missing or either enum value is
                unknown.
        """
        operation = data.get("operation")
        if operation is None:
            raise ValueError("operation log data is missing 'operation'")
        source = data.get("source")
        destination = data.get("destination")
        return cls(
            operation=FileOperation(str(operation)),
            source=Path(str(source)) if source is not None else None,
            destination=Path(str(destination)) if destination is not None else None,
            backups=_paths_from(data.get("backups")),
            created_paths=_paths_from(data.get("created_paths")),
            status=OperationStatus(
                str(data.get("status", OperationStatus.APPLIED.value)),
            ),
        )


def _paths_from(value: object) -> tuple[Path, ...]:
    """Decode a serialized path list.

    Args:
        value: JSON list of path strings, or ``None`` when absent.

    Returns:
        The paths as a tuple; empty when ``value`` is not a list.
    """
    if not isinstance(value, list | tuple):
        return ()
    return tuple(Path(str(item)) for item in value)
