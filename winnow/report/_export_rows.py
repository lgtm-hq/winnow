"""Row-insert helpers for :func:`winnow.report.export.export_run`.

Each helper issues its statements through the cursor of the caller's open
transaction and implements one slice of the export row mapping: the run row,
the media files, the duplicate groups with their member and best-file links,
and the filesystem operations.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from winnow.exceptions import ReportError
from winnow.models.media import MediaMetadata
from winnow.report._connection import MSG_NO_ROW_ID
from winnow.report.schema import TERMINAL_RUN_STATUSES

if TYPE_CHECKING:
    from winnow.report.export import RunExport

_OPERATION = "export_run"
_CHUNK_SIZE = 500
_CREATION_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_INSERT_RUN = (
    "INSERT INTO report_runs "
    "(id, root_path, status, completed_at, total_files, duplicate_count, notes) "
    "VALUES (?, ?, ?, "
    "CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END, ?, ?, ?);"
)
_INSERT_MEDIA_FILE = (
    "INSERT INTO media_files (run_id, path, filename, media_type, size_bytes, "
    "content_hash, creation_date, quality_score, metadata) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);"
)
_INSERT_GROUP = (
    "INSERT INTO duplicate_groups "
    "(run_id, group_number, media_type, file_count, target_path) "
    "VALUES (?, ?, ?, ?, ?);"
)
_INSERT_OPERATION = (
    "INSERT INTO operations "
    "(run_id, media_file_id, operation, status, source, destination) "
    "VALUES (?, ?, ?, ?, ?, ?);"
)


def _format_creation_date(dt: datetime) -> str:
    """Render a datetime as the UTC ``creation_date`` column value.

    Args:
        dt: Datetime to format; naive values are treated as UTC.

    Returns:
        The datetime as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC.
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime(_CREATION_DATE_FORMAT)


def _metadata_json(metadata: MediaMetadata | None) -> str | None:
    """Serialize media metadata for the ``metadata`` column.

    Args:
        metadata: Extracted metadata, if any.

    Returns:
        Compact JSON without ``None`` fields, or ``None`` when absent.
    """
    if metadata is None:
        return None
    return metadata.model_dump_json(exclude_none=True)


def _insert_run(
    cursor: sqlite3.Cursor,
    *,
    export: RunExport,
    run_id: int | None,
) -> int:
    """Insert the ``report_runs`` row, replacing an existing run if asked.

    Args:
        cursor: Cursor bound to the open export transaction.
        export: Run facts to persist.
        run_id: Existing run to replace, or ``None`` to create a new run.

    Returns:
        The identifier of the inserted run.

    Raises:
        ReportError: If SQLite does not report a row id for the insert.
    """
    if run_id is not None:
        cursor.execute("DELETE FROM report_runs WHERE id = ?;", (run_id,))
    cursor.execute(
        _INSERT_RUN,
        (
            run_id,
            str(export.root_path),
            str(export.status),
            export.status in TERMINAL_RUN_STATUSES,
            len(export.files),
            sum(len(group.files) for group in export.groups),
            export.notes,
        ),
    )
    inserted = cursor.lastrowid
    if inserted is None:
        raise ReportError(MSG_NO_ROW_ID, operation=_OPERATION)
    return inserted


def _insert_media_files(
    cursor: sqlite3.Cursor,
    *,
    export: RunExport,
    run_id: int,
) -> dict[str, int]:
    """Insert every media file and return the path to row id map.

    Args:
        cursor: Cursor bound to the open export transaction.
        export: Run facts to persist.
        run_id: Identifier of the owning run.

    Returns:
        Mapping from ``str(path)`` to the inserted ``media_files.id``.
    """
    rows = [
        (
            run_id,
            str(media.path),
            media.path.name,
            str(media.media_type),
            media.size_bytes,
            export.content_hashes.get(media.path),
            _format_creation_date(media.creation_date),
            export.quality_scores.get(media.path),
            _metadata_json(media.metadata),
        )
        for media in export.files
    ]
    for start in range(0, len(rows), _CHUNK_SIZE):
        cursor.executemany(_INSERT_MEDIA_FILE, rows[start : start + _CHUNK_SIZE])
    cursor.execute("SELECT id, path FROM media_files WHERE run_id = ?;", (run_id,))
    return {row["path"]: row["id"] for row in cursor.fetchall()}


def _insert_groups(
    cursor: sqlite3.Cursor,
    *,
    export: RunExport,
    run_id: int,
    file_ids: Mapping[str, int],
) -> None:
    """Insert duplicate groups and link members and best files to them.

    Args:
        cursor: Cursor bound to the open export transaction.
        export: Run facts to persist.
        run_id: Identifier of the owning run.
        file_ids: Mapping from ``str(path)`` to ``media_files.id``.
    """
    for group in export.groups:
        target = None if group.target_path is None else str(group.target_path)
        cursor.execute(
            _INSERT_GROUP,
            (
                run_id,
                group.group_number,
                str(group.media_type),
                len(group.files),
                target,
            ),
        )
        group_id = cursor.lastrowid
        cursor.executemany(
            "UPDATE media_files SET group_id = ? WHERE id = ?;",
            [(group_id, file_ids[str(member)]) for member in group.files],
        )
        best_file_id = None if target is None else file_ids.get(target)
        if best_file_id is not None:
            cursor.execute(
                "UPDATE duplicate_groups SET best_file_id = ? WHERE id = ?;",
                (best_file_id, group_id),
            )


def _insert_operations(
    cursor: sqlite3.Cursor,
    *,
    export: RunExport,
    run_id: int,
    file_ids: Mapping[str, int],
) -> None:
    """Insert filesystem operations, resolving each source to its file row.

    Args:
        cursor: Cursor bound to the open export transaction.
        export: Run facts to persist.
        run_id: Identifier of the owning run.
        file_ids: Mapping from ``str(path)`` to ``media_files.id``.
    """
    rows = [
        (
            run_id,
            None if log.source is None else file_ids.get(str(log.source)),
            str(log.operation),
            str(log.status),
            None if log.source is None else str(log.source),
            None if log.destination is None else str(log.destination),
        )
        for log in export.operations
    ]
    for start in range(0, len(rows), _CHUNK_SIZE):
        cursor.executemany(_INSERT_OPERATION, rows[start : start + _CHUNK_SIZE])


__all__ = [
    "_format_creation_date",
    "_insert_groups",
    "_insert_media_files",
    "_insert_operations",
    "_insert_run",
    "_metadata_json",
]
