"""SQLite-backed report database with schema versioning and CRUD helpers.

:class:`ReportDatabase` wraps a stdlib :mod:`sqlite3` connection and provides
typed create/read/update/delete helpers for report runs, media files,
duplicate groups, and filesystem operations. It owns schema provisioning and
version tracking, and exposes a full-text search over media file paths backed
by the FTS5 index defined in :mod:`winnow.report.schema`.

The database path is injectable; the default ``":memory:"`` keeps a private
in-memory database alive for the lifetime of the instance, which is convenient
for tests and ephemeral reporting.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from winnow.exceptions import ReportError
from winnow.fs.operations import FileOperation, OperationStatus
from winnow.models.media import MediaType
from winnow.report.records import (
    DuplicateGroupRecord,
    MediaFileRecord,
    OperationRecord,
    ReportRun,
)
from winnow.report.schema import (
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    RunStatus,
)


class ReportDatabase:
    """Persistence layer for winnow report data.

    Args:
        path: Filesystem path to the SQLite database, or ``":memory:"`` for a
            transient in-memory database. Defaults to ``":memory:"``.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._path: Path | str = path if path == ":memory:" else Path(path)
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path | str:
        """Return the configured database path.

        Returns:
            The database path, or ``":memory:"`` for in-memory databases.
        """
        return self._path

    @property
    def is_connected(self) -> bool:
        """Return whether an active connection is open.

        Returns:
            ``True`` when a connection has been opened and not yet closed.
        """
        return self._connection is not None

    def connect(self) -> Self:
        """Open the connection and provision the schema if needed.

        Returns:
            This database instance, to support fluent use.

        Raises:
            ReportError: If the connection cannot be opened.
        """
        if self._connection is not None:
            return self
        database = self._path if self._path == ":memory:" else str(self._path)
        try:
            connection = sqlite3.connect(database)
        except sqlite3.Error as error:
            raise ReportError(
                "could not open report database",
                operation="connect",
                file_path=None if self._path == ":memory:" else self._path,
            ) from error
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        self._connection = connection
        self._initialize_schema()
        return self

    def close(self) -> None:
        """Close the active connection, if any."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        """Open the connection on context entry.

        Returns:
            This connected database instance.
        """
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection on context exit.

        Args:
            exc_type: Exception type raised in the context, if any.
            exc_value: Exception instance raised in the context, if any.
            traceback: Active traceback, if any.
        """
        self.close()

    def _require_connection(self) -> sqlite3.Connection:
        """Return the active connection or fail if disconnected.

        Returns:
            The open SQLite connection.

        Raises:
            ReportError: If no connection is currently open.
        """
        if self._connection is None:
            raise ReportError(
                "report database is not connected",
                operation="require_connection",
            )
        return self._connection

    def _initialize_schema(self) -> None:
        """Provision the schema or validate an existing version.

        Raises:
            ReportError: If the stored schema version is unsupported.
        """
        current = self._read_schema_version()
        if current == SCHEMA_VERSION:
            return
        if current == 0:
            self._apply_schema()
            return
        raise ReportError(
            "unsupported report schema version",
            operation="initialize_schema",
            details={"found": current, "expected": SCHEMA_VERSION},
        )

    def _read_schema_version(self) -> int:
        """Return the persisted schema version, or ``0`` if unprovisioned.

        Returns:
            The highest recorded schema version, or ``0`` when the schema has
            not been created yet.
        """
        connection = self._require_connection()
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'schema_version';",
        ).fetchone()
        if table is None:
            return 0
        row = connection.execute(
            "SELECT MAX(version) AS version FROM schema_version;",
        ).fetchone()
        version = row["version"]
        return int(version) if version is not None else 0

    def _apply_schema(self) -> None:
        """Create all tables, indexes, and the FTS index, then record version.

        Raises:
            ReportError: If any DDL statement fails.
        """
        connection = self._require_connection()
        try:
            with connection:
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?);",
                    (SCHEMA_VERSION,),
                )
        except sqlite3.Error as error:
            raise ReportError(
                "could not provision report schema",
                operation="apply_schema",
            ) from error

    def schema_version(self) -> int:
        """Return the schema version stored in the database.

        Returns:
            The persisted schema version.
        """
        return self._read_schema_version()

    def _write(self, sql: str, params: Sequence[object]) -> sqlite3.Cursor:
        """Execute a write statement inside a transaction.

        Args:
            sql: Parameterized SQL statement to execute.
            params: Bound parameter values for the statement.

        Returns:
            The cursor produced by the statement.

        Raises:
            ReportError: If the statement fails.
        """
        connection = self._require_connection()
        try:
            with connection:
                return connection.execute(sql, params)
        except sqlite3.Error as error:
            raise ReportError(
                "report database write failed",
                operation="write",
                details={"statement": sql.strip().split("\n", 1)[0]},
            ) from error

    def _query(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> list[sqlite3.Row]:
        """Execute a read statement and fetch all rows.

        Args:
            sql: Parameterized SQL statement to execute.
            params: Bound parameter values for the statement.

        Returns:
            All rows produced by the query.

        Raises:
            ReportError: If the query fails.
        """
        connection = self._require_connection()
        try:
            return connection.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise ReportError(
                "report database query failed",
                operation="query",
                details={"statement": sql.strip().split("\n", 1)[0]},
            ) from error

    @staticmethod
    def _last_row_id(cursor: sqlite3.Cursor, *, operation: str) -> int:
        """Return the last inserted row id or fail.

        Args:
            cursor: Cursor produced by an insert statement.
            operation: Operation name used for error context.

        Returns:
            The primary key of the inserted row.

        Raises:
            ReportError: If no row id is available.
        """
        row_id = cursor.lastrowid
        if row_id is None:
            raise ReportError(
                "insert did not produce a row id",
                operation=operation,
            )
        return row_id

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

        Args:
            run_id: Identifier of the run to update.
            status: New lifecycle status.

        Returns:
            ``True`` if a run was updated, ``False`` otherwise.
        """
        cursor = self._write(
            "UPDATE report_runs SET status = ? WHERE id = ?;",
            (str(status), run_id),
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
        """
        cursor = self._write(
            "UPDATE report_runs SET "
            "status = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
            "total_files = COALESCE(?, total_files), "
            "duplicate_count = COALESCE(?, duplicate_count) "
            "WHERE id = ?;",
            (str(status), total_files, duplicate_count, run_id),
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

    def add_media_file(
        self,
        *,
        run_id: int,
        path: Path | str,
        media_type: MediaType | str,
        size_bytes: int = 0,
        content_hash: str | None = None,
        creation_date: str | None = None,
        group_id: int | None = None,
        filename: str | None = None,
    ) -> int:
        """Insert a media file discovered during a run.

        Args:
            run_id: Owning run identifier.
            path: Path to the media file.
            media_type: Media type classification.
            size_bytes: File size in bytes.
            content_hash: Content hash for exact-duplicate detection, if any.
            creation_date: ISO-8601 creation timestamp, if known.
            group_id: Duplicate group identifier, if the file is a duplicate.
            filename: Explicit file name; derived from ``path`` when omitted.

        Returns:
            The identifier of the created media file.
        """
        resolved_name = filename if filename is not None else Path(path).name
        cursor = self._write(
            "INSERT INTO media_files "
            "(run_id, group_id, path, filename, media_type, size_bytes, "
            "content_hash, creation_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            (
                run_id,
                group_id,
                str(path),
                resolved_name,
                str(media_type),
                size_bytes,
                content_hash,
                creation_date,
            ),
        )
        return self._last_row_id(cursor, operation="add_media_file")

    def get_media_file(self, file_id: int) -> MediaFileRecord | None:
        """Fetch a media file by identifier.

        Args:
            file_id: Identifier of the media file to fetch.

        Returns:
            The matching media file, or ``None`` if none exists.
        """
        rows = self._query(
            "SELECT * FROM media_files WHERE id = ?;",
            (file_id,),
        )
        return MediaFileRecord.from_row(rows[0]) if rows else None

    def list_media_files(self, run_id: int) -> list[MediaFileRecord]:
        """List media files for a run ordered by path.

        Args:
            run_id: Identifier of the owning run.

        Returns:
            The run's media files, in path order.
        """
        rows = self._query(
            "SELECT * FROM media_files WHERE run_id = ? ORDER BY path ASC, id ASC;",
            (run_id,),
        )
        return [MediaFileRecord.from_row(row) for row in rows]

    def assign_media_file_group(
        self,
        file_id: int,
        *,
        group_id: int | None,
    ) -> bool:
        """Assign or clear the duplicate group of a media file.

        Args:
            file_id: Identifier of the media file to update.
            group_id: Duplicate group identifier, or ``None`` to clear it.

        Returns:
            ``True`` if a media file was updated, ``False`` otherwise.
        """
        cursor = self._write(
            "UPDATE media_files SET group_id = ? WHERE id = ?;",
            (group_id, file_id),
        )
        return cursor.rowcount > 0

    @staticmethod
    def _build_match_query(query: str) -> str:
        """Convert free-text into a safe FTS5 term query.

        Whitespace-separated terms are wrapped as quoted phrases and combined
        with an implicit AND. Quoting escapes FTS5 operators (including
        characters such as ``.``) so that ordinary filename fragments do not
        raise syntax errors.

        Args:
            query: Free-text query to convert.

        Returns:
            A quoted FTS5 match expression covering all terms.
        """
        terms = query.split()
        return " ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

    def search_media_files(
        self,
        query: str,
        *,
        run_id: int | None = None,
        raw: bool = False,
    ) -> list[MediaFileRecord]:
        """Full-text search media files by path or filename.

        Args:
            query: Search text. Treated as space-separated literal terms unless
                ``raw`` is set, in which case it is passed through as an FTS5
                match expression.
            run_id: Restrict results to a single run when provided.
            raw: Pass ``query`` directly as an FTS5 match expression.

        Returns:
            Matching media files ordered by FTS relevance. An empty query
            yields no results.
        """
        match_query = query if raw else self._build_match_query(query)
        if not match_query.strip():
            return []
        sql = (
            "SELECT media_files.* FROM media_files_fts "
            "JOIN media_files ON media_files.id = media_files_fts.rowid "
            "WHERE media_files_fts MATCH ?"
        )
        params: list[object] = [match_query]
        if run_id is not None:
            sql += " AND media_files.run_id = ?"
            params.append(run_id)
        sql += " ORDER BY rank;"
        rows = self._query(sql, params)
        return [MediaFileRecord.from_row(row) for row in rows]

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
            media_file_id: Related media file identifier, if any.
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


__all__ = ["ReportDatabase"]
