"""Connection and schema lifecycle for the report database.

:class:`ConnectionManager` owns the stdlib :mod:`sqlite3` connection, schema
provisioning and version validation, and the low-level write/query helpers
shared by the per-entity stores that compose
:class:`winnow.report.database.ReportDatabase`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from winnow.exceptions import ReportError, StorageError
from winnow.report.schema import MIGRATIONS, SCHEMA_STATEMENTS, SCHEMA_VERSION
from winnow.storage import apply_schema, read_schema_version
from winnow.storage.migrations import MSG_MIGRATION_GAP, MSG_NEWER_THAN_BUILD

MSG_OPEN_FAILED = "could not open report database"
MSG_NOT_CONNECTED = "report database is not connected"
MSG_UNSUPPORTED_VERSION = "unsupported report schema version"
MSG_PROVISION_FAILED = "could not provision report schema"
MSG_WRITE_FAILED = "report database write failed"
MSG_QUERY_FAILED = "report database query failed"
MSG_NO_ROW_ID = "insert did not produce a row id"

_VERSION_MISMATCH_MESSAGES = frozenset({MSG_NEWER_THAN_BUILD, MSG_MIGRATION_GAP})


class ConnectionManager:
    """Own the SQLite connection, schema lifecycle, and statement helpers.

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

        If schema validation or provisioning fails, the freshly opened
        connection is closed and cleared before the error propagates, so the
        instance never reports itself connected after a failed initialization.

        Returns:
            This database instance, to support fluent use.

        Raises:
            ReportError: If the connection cannot be opened or the schema
                cannot be initialized.
        """
        if self._connection is not None:
            return self
        database = self._path if self._path == ":memory:" else str(self._path)
        try:
            connection = sqlite3.connect(database)
        except sqlite3.Error as error:
            raise ReportError(
                MSG_OPEN_FAILED,
                operation="connect",
                file_path=None if self._path == ":memory:" else self._path,
            ) from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON;")
            self._connection = connection
            self._initialize_schema()
        except Exception:
            self._connection = None
            connection.close()
            raise
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
                MSG_NOT_CONNECTED,
                operation="require_connection",
            )
        return self._connection

    def _initialize_schema(self) -> None:
        """Provision the schema or migrate an existing database to it.

        Delegates to :func:`winnow.storage.apply_schema` and maps its
        :class:`StorageError` onto the :class:`ReportError` contract.

        Raises:
            ReportError: If the stored schema version is unsupported (newer
                than this build, or no migration path) or provisioning fails.
        """
        connection = self._require_connection()
        try:
            apply_schema(
                connection,
                baseline=SCHEMA_STATEMENTS,
                migrations=MIGRATIONS,
                target_version=SCHEMA_VERSION,
            )
        except StorageError as error:
            if error.message in _VERSION_MISMATCH_MESSAGES:
                raise ReportError(
                    MSG_UNSUPPORTED_VERSION,
                    operation="initialize_schema",
                    details=error.context.details,
                ) from error
            raise ReportError(
                MSG_PROVISION_FAILED,
                operation="apply_schema",
                details=error.context.details,
            ) from error

    def schema_version(self) -> int:
        """Return the schema version stored in the database.

        Returns:
            The persisted schema version.
        """
        return read_schema_version(self._require_connection())

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Run a block of statements inside one explicit transaction.

        Opens a ``BEGIN IMMEDIATE`` transaction, yields a cursor bound to it,
        commits when the block exits normally, and rolls back when the block
        raises. Callers issue every statement through the yielded cursor so
        the whole block is persisted or discarded as a unit.

        Yields:
            A cursor on the open transaction.

        Raises:
            ReportError: If the transaction cannot be started or committed, or
                if a statement inside the block fails; the transaction is
                rolled back before the error propagates.
        """
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE;")
        except sqlite3.Error as error:
            raise ReportError(
                MSG_WRITE_FAILED,
                operation="transaction",
                details={"phase": "begin"},
            ) from error
        try:
            yield connection.cursor()
            connection.execute("COMMIT;")
        except sqlite3.Error as error:
            connection.rollback()
            raise ReportError(
                MSG_WRITE_FAILED,
                operation="transaction",
            ) from error
        except BaseException:
            connection.rollback()
            raise

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
                MSG_WRITE_FAILED,
                operation="write",
                details={"statement": sql.strip().split("\n", 1)[0]},
            ) from error

    def _write_many(
        self,
        statements: Sequence[tuple[str, Sequence[object]]],
    ) -> sqlite3.Cursor:
        """Execute several write statements inside a single transaction.

        Args:
            statements: Ordered ``(sql, params)`` pairs to execute atomically.

        Returns:
            The cursor produced by the final statement.

        Raises:
            ReportError: If any statement fails; the transaction rolls back.
        """
        connection = self._require_connection()
        try:
            with connection:
                cursor = connection.cursor()
                for sql, params in statements:
                    cursor.execute(sql, params)
                return cursor
        except sqlite3.Error as error:
            raise ReportError(
                MSG_WRITE_FAILED,
                operation="write_many",
                details={"statements": len(statements)},
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
                MSG_QUERY_FAILED,
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
                MSG_NO_ROW_ID,
                operation=operation,
            )
        return row_id


__all__ = ["ConnectionManager"]
