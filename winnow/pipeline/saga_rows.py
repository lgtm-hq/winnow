"""Row codecs, timestamps and identifiers for the saga session log."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from winnow.pipeline.saga_records import (
    CommandRecord,
    CommandStatus,
    SessionRecord,
    SessionStatus,
)

SELECT_SESSIONS = """
SELECT s.session_id, s.started_at, s.completed_at, s.status, s.config_digest,
       s.source, s.destination,
       (SELECT COUNT(*) FROM commands c WHERE c.session_id = s.session_id)
           AS command_count
FROM sessions s
"""
SELECT_COMMANDS = (
    "SELECT seq, session_id, command_type, args_json, log_json, status, "
    "started_at, completed_at FROM commands"
)


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Returns:
        Timestamp with microsecond precision and a ``+00:00`` offset.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


def to_iso(moment: datetime) -> str:
    """Normalize a datetime to the log's timestamp format.

    Args:
        moment: Cut-off instant; naive values are taken as UTC.

    Returns:
        ISO-8601 UTC string comparable with stored timestamps.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def new_session_id() -> str:
    """Return a sortable, collision-resistant session identifier.

    Returns:
        ``YYYYmmddTHHMMSS-<8 hex chars>``.
    """
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"


def session_from_row(row: sqlite3.Row) -> SessionRecord:
    """Build a :class:`SessionRecord` from a joined ``sessions`` row.

    Args:
        row: Row produced by :data:`SELECT_SESSIONS`.

    Returns:
        Immutable session record.
    """
    return SessionRecord(
        session_id=row["session_id"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        status=SessionStatus(row["status"]),
        command_count=int(row["command_count"]),
        config_digest=row["config_digest"],
        source=row["source"],
        destination=row["destination"],
    )


def command_from_row(row: sqlite3.Row) -> CommandRecord:
    """Build a :class:`CommandRecord` from a ``commands`` row.

    Args:
        row: Row produced by :data:`SELECT_COMMANDS`.

    Returns:
        Immutable command record with JSON columns decoded.
    """
    log_json = row["log_json"]
    return CommandRecord(
        seq=int(row["seq"]),
        session_id=row["session_id"],
        command_type=row["command_type"],
        args=json.loads(row["args_json"]),
        log=None if log_json is None else json.loads(log_json),
        status=CommandStatus(row["status"]),
        timestamp=row["started_at"],
        completed_at=row["completed_at"],
    )


__all__ = [
    "SELECT_COMMANDS",
    "SELECT_SESSIONS",
    "command_from_row",
    "new_session_id",
    "session_from_row",
    "to_iso",
    "utc_now",
]
