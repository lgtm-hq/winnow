"""Report-run CRUD for the report database.

:class:`RunStore` provides the run lifecycle helpers mixed into
:class:`winnow.report.database.ReportDatabase`. Terminal transitions are
routed through :meth:`RunStore.complete_run` so ``completed_at`` is always
consistent with the stored status.
"""

from __future__ import annotations

from pathlib import Path

from winnow.exceptions import ReportError
from winnow.report._connection import ConnectionManager
from winnow.report.records import ReportRun
from winnow.report.schema import TERMINAL_RUN_STATUSES, RunStatus

MSG_INVALID_RUN_STATUS = "unknown run status"
MSG_NONTERMINAL_COMPLETE = "complete_run requires a terminal status"


def _coerce_run_status(status: RunStatus | str, *, operation: str) -> RunStatus:
    """Normalize a status argument into a :class:`RunStatus` member.

    Args:
        status: Enum member or raw status string to normalize.
        operation: Operation name used for error context.

    Returns:
        The matching :class:`RunStatus` member.

    Raises:
        ReportError: If the value does not name a known run status.
    """
    if isinstance(status, RunStatus):
        return status
    try:
        return RunStatus(status)
    except ValueError as error:
        raise ReportError(
            MSG_INVALID_RUN_STATUS,
            operation=operation,
            details={"status": str(status)},
        ) from error


class RunStore(ConnectionManager):
    """CRUD and lifecycle helpers for the ``report_runs`` table."""

    def create_run(
        self,
        *,
        root_path: Path | str,
        status: RunStatus | str = RunStatus.RUNNING,
        notes: str | None = None,
    ) -> int:
        """Insert a new report run.

        Args:
            root_path: Root directory scanned by the run.
            status: Initial lifecycle status of the run.
            notes: Optional free-form annotation.

        Returns:
            The identifier of the created run.
        """
        cursor = self._write(
            "INSERT INTO report_runs (root_path, status, notes) VALUES (?, ?, ?);",
            (str(root_path), str(status), notes),
        )
        return self._last_row_id(cursor, operation="create_run")

    def get_run(self, run_id: int) -> ReportRun | None:
        """Fetch a single report run by identifier.

        Args:
            run_id: Identifier of the run to fetch.

        Returns:
            The matching run, or ``None`` if no run exists.
        """
        rows = self._query(
            "SELECT * FROM report_runs WHERE id = ?;",
            (run_id,),
        )
        return ReportRun.from_row(rows[0]) if rows else None

    def list_runs(self) -> list[ReportRun]:
        """List all report runs ordered by start time.

        Returns:
            All recorded runs, oldest first.
        """
        rows = self._query(
            "SELECT * FROM report_runs ORDER BY started_at ASC, id ASC;",
        )
        return [ReportRun.from_row(row) for row in rows]

    def update_run_status(
        self,
        run_id: int,
        *,
        status: RunStatus | str,
    ) -> bool:
        """Update the lifecycle status of a run.

        Terminal statuses are routed through :meth:`complete_run` so that
        ``completed_at`` is recorded; moving a run back to a nonterminal
        status clears ``completed_at``.

        Args:
            run_id: Identifier of the run to update.
            status: New lifecycle status.

        Returns:
            ``True`` if a run was updated, ``False`` otherwise.

        Raises:
            ReportError: If the status is not a known run status.
        """
        resolved = _coerce_run_status(status, operation="update_run_status")
        if resolved in TERMINAL_RUN_STATUSES:
            return self.complete_run(run_id, status=resolved)
        cursor = self._write(
            "UPDATE report_runs SET status = ?, completed_at = NULL WHERE id = ?;",
            (str(resolved), run_id),
        )
        return cursor.rowcount > 0

    def complete_run(
        self,
        run_id: int,
        *,
        status: RunStatus | str = RunStatus.COMPLETED,
        total_files: int | None = None,
        duplicate_count: int | None = None,
    ) -> bool:
        """Mark a run complete and update its summary counters.

        Args:
            run_id: Identifier of the run to complete.
            status: Terminal status to assign to the run.
            total_files: Total media files recorded, if updating.
            duplicate_count: Total duplicate files recorded, if updating.

        Returns:
            ``True`` if a run was updated, ``False`` otherwise.

        Raises:
            ReportError: If the status is unknown or not terminal.
        """
        resolved = _coerce_run_status(status, operation="complete_run")
        if resolved not in TERMINAL_RUN_STATUSES:
            raise ReportError(
                MSG_NONTERMINAL_COMPLETE,
                operation="complete_run",
                details={"status": str(resolved)},
            )
        cursor = self._write(
            "UPDATE report_runs SET "
            "status = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
            "total_files = COALESCE(?, total_files), "
            "duplicate_count = COALESCE(?, duplicate_count) "
            "WHERE id = ?;",
            (str(resolved), total_files, duplicate_count, run_id),
        )
        return cursor.rowcount > 0

    def delete_run(self, run_id: int) -> bool:
        """Delete a run and all rows that cascade from it.

        Args:
            run_id: Identifier of the run to delete.

        Returns:
            ``True`` if a run was deleted, ``False`` otherwise.
        """
        cursor = self._write(
            "DELETE FROM report_runs WHERE id = ?;",
            (run_id,),
        )
        return cursor.rowcount > 0


__all__ = ["RunStore"]
