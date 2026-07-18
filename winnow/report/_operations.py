"""Filesystem-operation logging for the report database.

:class:`OperationStore` provides the operation-log helpers mixed into
:class:`winnow.report.database.ReportDatabase`.
"""

from __future__ import annotations

from pathlib import Path

from winnow.fs.operations import FileOperation, OperationStatus
from winnow.report._connection import ConnectionManager
from winnow.report.records import OperationRecord


class OperationStore(ConnectionManager):
    """CRUD helpers for the ``operations`` table."""

    def add_operation(
        self,
        *,
        run_id: int,
        operation: FileOperation | str,
        status: OperationStatus | str,
        media_file_id: int | None = None,
        source: Path | str | None = None,
        destination: Path | str | None = None,
    ) -> int:
        """Record a filesystem operation performed during a run.

        Args:
            run_id: Owning run identifier.
            operation: Operation type that was applied.
            status: Lifecycle state of the operation.
            media_file_id: Related media file identifier, if any. Must belong
                to the same run, enforced by a composite foreign key.
            source: Source path used by the operation, if any.
            destination: Destination path used by the operation, if any.

        Returns:
            The identifier of the created operation record.
        """
        cursor = self._write(
            "INSERT INTO operations "
            "(run_id, media_file_id, operation, status, source, destination) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (
                run_id,
                media_file_id,
                str(operation),
                str(status),
                None if source is None else str(source),
                None if destination is None else str(destination),
            ),
        )
        return self._last_row_id(cursor, operation="add_operation")

    def list_operations(self, run_id: int) -> list[OperationRecord]:
        """List operations recorded for a run in insertion order.

        Args:
            run_id: Identifier of the owning run.

        Returns:
            The run's operations, oldest first.
        """
        rows = self._query(
            "SELECT * FROM operations WHERE run_id = ? "
            "ORDER BY created_at ASC, id ASC;",
            (run_id,),
        )
        return [OperationRecord.from_row(row) for row in rows]


__all__ = ["OperationStore"]
