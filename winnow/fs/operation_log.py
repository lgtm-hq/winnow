"""Structured log entries for filesystem operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
