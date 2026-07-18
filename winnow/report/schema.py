"""SQLite v2 report schema definitions.

This module centralizes the data-definition language (DDL) for the winnow
report database. It defines the current :data:`SCHEMA_VERSION`, the ordered
statements that build each table, the FTS5 full-text index (with the triggers
that keep it synchronized), and the secondary indexes used by common queries.

The statements are intentionally idempotent (``IF NOT EXISTS``) so that
initializing an already-provisioned database is a safe no-op.
"""

from __future__ import annotations

from enum import StrEnum, auto

SCHEMA_VERSION = 2
"""Current report schema version persisted in the ``schema_version`` table."""


class RunStatus(StrEnum):
    """Lifecycle states for a recorded report run."""

    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()


CREATE_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_REPORT_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS report_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    total_files INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
"""

CREATE_DUPLICATE_GROUPS_TABLE = """
CREATE TABLE IF NOT EXISTS duplicate_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL
        REFERENCES report_runs(id) ON DELETE CASCADE,
    group_number INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    target_path TEXT
);
"""

CREATE_MEDIA_FILES_TABLE = """
CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL
        REFERENCES report_runs(id) ON DELETE CASCADE,
    group_id INTEGER
        REFERENCES duplicate_groups(id) ON DELETE SET NULL,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    creation_date TEXT
);
"""

CREATE_OPERATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL
        REFERENCES report_runs(id) ON DELETE CASCADE,
    media_file_id INTEGER
        REFERENCES media_files(id) ON DELETE SET NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT,
    destination TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_MEDIA_FILES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS media_files_fts USING fts5(
    path,
    filename,
    content='media_files',
    content_rowid='id'
);
"""

CREATE_MEDIA_FILES_FTS_INSERT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS media_files_fts_ai
AFTER INSERT ON media_files BEGIN
    INSERT INTO media_files_fts(rowid, path, filename)
    VALUES (new.id, new.path, new.filename);
END;
"""

CREATE_MEDIA_FILES_FTS_DELETE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS media_files_fts_ad
AFTER DELETE ON media_files BEGIN
    INSERT INTO media_files_fts(media_files_fts, rowid, path, filename)
    VALUES ('delete', old.id, old.path, old.filename);
END;
"""

CREATE_MEDIA_FILES_FTS_UPDATE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS media_files_fts_au
AFTER UPDATE ON media_files BEGIN
    INSERT INTO media_files_fts(media_files_fts, rowid, path, filename)
    VALUES ('delete', old.id, old.path, old.filename);
    INSERT INTO media_files_fts(rowid, path, filename)
    VALUES (new.id, new.path, new.filename);
END;
"""

CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_report_runs_status ON report_runs(status);",
    "CREATE INDEX IF NOT EXISTS idx_media_files_run_id ON media_files(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_media_files_group_id ON media_files(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_media_files_content_hash "
    "ON media_files(content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_media_files_media_type ON media_files(media_type);",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_groups_run_id "
    "ON duplicate_groups(run_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_duplicate_groups_run_number "
    "ON duplicate_groups(run_id, group_number);",
    "CREATE INDEX IF NOT EXISTS idx_operations_run_id ON operations(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_operations_media_file_id "
    "ON operations(media_file_id);",
)
"""Secondary indexes optimizing foreign-key joins and common filters."""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_SCHEMA_VERSION_TABLE,
    CREATE_REPORT_RUNS_TABLE,
    CREATE_DUPLICATE_GROUPS_TABLE,
    CREATE_MEDIA_FILES_TABLE,
    CREATE_OPERATIONS_TABLE,
    CREATE_MEDIA_FILES_FTS,
    CREATE_MEDIA_FILES_FTS_INSERT_TRIGGER,
    CREATE_MEDIA_FILES_FTS_DELETE_TRIGGER,
    CREATE_MEDIA_FILES_FTS_UPDATE_TRIGGER,
    *CREATE_INDEXES,
)
"""Ordered DDL statements that provision the full report schema."""


__all__ = [
    "CREATE_INDEXES",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "RunStatus",
]
