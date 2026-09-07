"""SQLite schema for the saga session log (``sessions.db``).

Versioning goes through :func:`winnow.storage.apply_schema`: bumping
:data:`SCHEMA_VERSION` requires updating the baseline in
:data:`SCHEMA_STATEMENTS` **and** appending a
:class:`~winnow.storage.Migration` to :data:`MIGRATIONS`, mirroring
:mod:`winnow.report.schema`.
"""

from __future__ import annotations

from winnow.pipeline.saga_records import CommandStatus, SessionStatus
from winnow.storage import Migration

SCHEMA_VERSION = 1
"""Current saga log schema version persisted in ``schema_version``."""

ARGS_SCHEMA_VERSION = 1
"""Version tag stored alongside serialized command arguments."""


def _status_check(column: str, values: type[SessionStatus | CommandStatus]) -> str:
    """Render a ``CHECK`` constraint restricting ``column`` to enum values.

    Args:
        column: Column the constraint applies to.
        values: Enum whose members are the permitted values.

    Returns:
        SQL ``CHECK (...)`` clause.
    """
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return f"CHECK ({column} IN ({allowed}))"


CREATE_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_SESSIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL {_status_check("status", SessionStatus)},
    config_digest TEXT NOT NULL,
    source TEXT NOT NULL,
    destination TEXT NOT NULL
);
"""

CREATE_COMMANDS_TABLE = f"""
CREATE TABLE IF NOT EXISTS commands (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL
        REFERENCES sessions(session_id) ON DELETE CASCADE,
    command_type TEXT NOT NULL,
    args_json TEXT NOT NULL,
    log_json TEXT,
    status TEXT NOT NULL {_status_check("status", CommandStatus)},
    started_at TEXT NOT NULL,
    completed_at TEXT
);
"""

CREATE_COMMANDS_SESSION_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_commands_session_id ON commands(session_id);"
)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_SCHEMA_VERSION_TABLE,
    CREATE_SESSIONS_TABLE,
    CREATE_COMMANDS_TABLE,
    CREATE_COMMANDS_SESSION_INDEX,
)
"""Ordered DDL statements that provision the full saga log schema."""

MIGRATIONS: tuple[Migration, ...] = ()
"""Stepwise upgrades to :data:`SCHEMA_VERSION`; empty until the first bump."""


__all__ = [
    "ARGS_SCHEMA_VERSION",
    "MIGRATIONS",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
]
