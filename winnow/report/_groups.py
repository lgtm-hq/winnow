"""Duplicate-group CRUD for the report database.

:class:`GroupStore` provides the duplicate-group helpers mixed into
:class:`winnow.report.database.ReportDatabase`, including best-file selection
and transactional group deletion that detaches member files first.
"""

from __future__ import annotations

from pathlib import Path

from winnow.models.media import MediaType
from winnow.report._connection import ConnectionManager
from winnow.report.records import DuplicateGroupRecord


class GroupStore(ConnectionManager):
    """CRUD helpers for the ``duplicate_groups`` table."""

    def add_duplicate_group(
        self,
        *,
        run_id: int,
        group_number: int,
        media_type: MediaType | str,
        file_count: int = 0,
        target_path: Path | str | None = None,
    ) -> int:
        """Insert a duplicate group for a run.

        Args:
            run_id: Owning run identifier.
            group_number: Stable group ordinal within the run.
            media_type: Media type shared by the group members.
            file_count: Number of files assigned to the group.
            target_path: Path selected as the retained copy, if any.

        Returns:
            The identifier of the created group.
        """
        cursor = self._write(
            "INSERT INTO duplicate_groups "
            "(run_id, group_number, media_type, file_count, target_path) "
            "VALUES (?, ?, ?, ?, ?);",
            (
                run_id,
                group_number,
                str(media_type),
                file_count,
                None if target_path is None else str(target_path),
            ),
        )
        return self._last_row_id(cursor, operation="add_duplicate_group")

    def get_duplicate_group(
        self,
        group_id: int,
    ) -> DuplicateGroupRecord | None:
        """Fetch a duplicate group by identifier.

        Args:
            group_id: Identifier of the group to fetch.

        Returns:
            The matching group, or ``None`` if no group exists.
        """
        rows = self._query(
            "SELECT * FROM duplicate_groups WHERE id = ?;",
            (group_id,),
        )
        return DuplicateGroupRecord.from_row(rows[0]) if rows else None

    def list_duplicate_groups(
        self,
        run_id: int,
    ) -> list[DuplicateGroupRecord]:
        """List duplicate groups for a run ordered by group number.

        Args:
            run_id: Identifier of the owning run.

        Returns:
            The run's duplicate groups, in group-number order.
        """
        rows = self._query(
            "SELECT * FROM duplicate_groups WHERE run_id = ? "
            "ORDER BY group_number ASC;",
            (run_id,),
        )
        return [DuplicateGroupRecord.from_row(row) for row in rows]

    def set_group_best_file(
        self,
        group_id: int,
        *,
        file_id: int | None,
    ) -> bool:
        """Set or clear the best (retained) file of a duplicate group.

        The composite foreign key on ``duplicate_groups`` guarantees the file
        belongs to the same run as the group.

        Args:
            group_id: Identifier of the group to update.
            file_id: Media file identifier to mark as best, or ``None``.

        Returns:
            ``True`` if a group was updated, ``False`` otherwise.

        Raises:
            ReportError: If the file does not exist in the group's run.
        """
        cursor = self._write(
            "UPDATE duplicate_groups SET best_file_id = ? WHERE id = ?;",
            (file_id, group_id),
        )
        return cursor.rowcount > 0

    def delete_duplicate_group(self, group_id: int) -> bool:
        """Delete a duplicate group, detaching its member files first.

        Member media files are kept but their ``group_id`` is cleared inside
        the same transaction, preserving the run-scoped foreign keys.

        Args:
            group_id: Identifier of the group to delete.

        Returns:
            ``True`` if a group was deleted, ``False`` otherwise.
        """
        cursor = self._write_many(
            (
                (
                    "UPDATE media_files SET group_id = NULL WHERE group_id = ?;",
                    (group_id,),
                ),
                (
                    "DELETE FROM duplicate_groups WHERE id = ?;",
                    (group_id,),
                ),
            ),
        )
        return cursor.rowcount > 0


__all__ = ["GroupStore"]
