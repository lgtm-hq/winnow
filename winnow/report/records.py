"""Row-backed record types for the report database.

Each dataclass mirrors a table in the report schema and provides a
:meth:`from_row` constructor that maps a :class:`sqlite3.Row` into a typed,
attribute-addressable record. These records are the read models returned by
:class:`winnow.report.database.ReportDatabase` query helpers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class ReportRun:
    """Persisted metadata about a single winnow report run.

    Args:
        id: Auto-incrementing primary key.
        root_path: Root directory that the run scanned.
        status: Lifecycle status of the run.
        started_at: ISO-8601 timestamp when the run began.
        completed_at: ISO-8601 timestamp when the run finished, if any.
        total_files: Number of media files recorded for the run.
        duplicate_count: Number of duplicate files recorded for the run.
        notes: Optional free-form annotation.
    """

    id: int
    root_path: str
    status: str
    started_at: str
    completed_at: str | None
    total_files: int
    duplicate_count: int
    notes: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build a :class:`ReportRun` from a database row.

        Args:
            row: Row selected from the ``report_runs`` table.

        Returns:
            A populated :class:`ReportRun` record.
        """
        return cls(
            id=row["id"],
            root_path=row["root_path"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            total_files=row["total_files"],
            duplicate_count=row["duplicate_count"],
            notes=row["notes"],
        )


@dataclass(frozen=True, slots=True)
class DuplicateGroupRecord:
    """Persisted duplicate group belonging to a report run.

    Args:
        id: Auto-incrementing primary key.
        run_id: Owning report run identifier.
        group_number: Stable group ordinal within the run.
        media_type: Media type shared by the group members.
        file_count: Number of files assigned to the group.
        target_path: Path selected as the retained copy, if any.
    """

    id: int
    run_id: int
    group_number: int
    media_type: str
    file_count: int
    target_path: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build a :class:`DuplicateGroupRecord` from a database row.

        Args:
            row: Row selected from the ``duplicate_groups`` table.

        Returns:
            A populated :class:`DuplicateGroupRecord` record.
        """
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            group_number=row["group_number"],
            media_type=row["media_type"],
            file_count=row["file_count"],
            target_path=row["target_path"],
        )


@dataclass(frozen=True, slots=True)
class MediaFileRecord:
    """Persisted media file discovered during a report run.

    Args:
        id: Auto-incrementing primary key.
        run_id: Owning report run identifier.
        group_id: Duplicate group identifier, if the file is a duplicate.
        path: Absolute or scan-relative path to the file.
        filename: File name component used for full-text search.
        media_type: Media type classification.
        size_bytes: File size in bytes.
        content_hash: Content hash used for exact-duplicate detection, if any.
        creation_date: ISO-8601 creation timestamp of the file, if known.
    """

    id: int
    run_id: int
    group_id: int | None
    path: str
    filename: str
    media_type: str
    size_bytes: int
    content_hash: str | None
    creation_date: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build a :class:`MediaFileRecord` from a database row.

        Args:
            row: Row selected from the ``media_files`` table.

        Returns:
            A populated :class:`MediaFileRecord` record.
        """
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            group_id=row["group_id"],
            path=row["path"],
            filename=row["filename"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            content_hash=row["content_hash"],
            creation_date=row["creation_date"],
        )


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """Persisted filesystem operation recorded for a report run.

    Args:
        id: Auto-incrementing primary key.
        run_id: Owning report run identifier.
        media_file_id: Related media file identifier, if any.
        operation: Operation type that was applied.
        status: Lifecycle state of the operation.
        source: Source path used by the operation, if any.
        destination: Destination path used by the operation, if any.
        created_at: ISO-8601 timestamp when the operation was recorded.
    """

    id: int
    run_id: int
    media_file_id: int | None
    operation: str
    status: str
    source: str | None
    destination: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build an :class:`OperationRecord` from a database row.

        Args:
            row: Row selected from the ``operations`` table.

        Returns:
            A populated :class:`OperationRecord` record.
        """
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            media_file_id=row["media_file_id"],
            operation=row["operation"],
            status=row["status"],
            source=row["source"],
            destination=row["destination"],
            created_at=row["created_at"],
        )


__all__ = [
    "DuplicateGroupRecord",
    "MediaFileRecord",
    "OperationRecord",
    "ReportRun",
]
