"""Connection ownership, schema provisioning and SQL plumbing for the saga log.

:class:`SagaStore` is the single :mod:`sqlite3` connection behind
:class:`winnow.pipeline.saga_log.SagaLog`. It knows how to open the database,
bring its schema current through :func:`winnow.storage.apply_schema`, run
writes inside ``BEGIN IMMEDIATE``, and map :mod:`sqlite3` failures onto
:class:`~winnow.exceptions.SagaError`; it knows nothing about sessions or
commands.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from winnow.config.defaults import user_data_dir
from winnow.exceptions import SagaError, StorageError
from winnow.pipeline.saga_schema import MIGRATIONS, SCHEMA_STATEMENTS, SCHEMA_VERSION
from winnow.storage import apply_schema

MEMORY_PATH = ":memory:"
DATABASE_NAME = "sessions.db"
CONNECT_TIMEOUT = 5.0
"""Seconds to wait for a locked database before giving up."""

MSG_LOCKED = "another winnow session holds the transaction log"
MSG_OPEN_FAILED = "could not open saga transaction log"
MSG_SCHEMA_FAILED = "could not provision saga transaction log schema"
MSG_WRITE_FAILED = "saga transaction log write failed"
MSG_QUERY_FAILED = "saga transaction log query failed"
MSG_CLOSED = "saga transaction log is closed"


class SagaStore:
    """Own the SQLite connection and statement helpers for the saga log.

    Args:
        path: Database file, ``":memory:"``, or ``None`` for
            ``user_data_dir() / "sessions.db"`` (parents created on demand).

    Raises:
        SagaError: If the database cannot be opened or its schema provisioned.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path: Path | str = _resolve_path(path)
        self._connection: sqlite3.Connection | None = _open(self.path)
        try:
            self._initialize_schema()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the connection; later calls raise :class:`SagaError`."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self, operation: str) -> Iterator[sqlite3.Connection]:
        """Run a write inside ``BEGIN IMMEDIATE ... COMMIT``.

        Args:
            operation: Calling method name, used for error context.

        Yields:
            The open connection with the write lock held.

        Raises:
            SagaError: ``MSG_LOCKED`` when another connection holds the write
                lock past :data:`CONNECT_TIMEOUT`; ``MSG_WRITE_FAILED`` for
                any other :class:`sqlite3.Error`. The transaction is rolled
                back in both cases.
        """
        connection = self._require_connection(operation)
        try:
            connection.execute("BEGIN IMMEDIATE;")
            yield connection
            connection.execute("COMMIT;")
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK;")
            raise _wrap_sqlite_error(error, operation=operation) from error

    def query(
        self,
        operation: str,
        sql: str,
        params: Sequence[object] = (),
    ) -> list[sqlite3.Row]:
        """Execute a read statement and fetch every row.

        Args:
            operation: Calling method name, used for error context.
            sql: Parameterized statement.
            params: Bound parameters.

        Returns:
            All rows produced by the statement.

        Raises:
            SagaError: If the statement fails.
        """
        connection = self._require_connection(operation)
        try:
            return connection.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise SagaError(
                MSG_QUERY_FAILED,
                operation=f"saga_log.{operation}",
            ) from error

    def _require_connection(self, operation: str) -> sqlite3.Connection:
        """Return the open connection or fail.

        Args:
            operation: Calling method name, used for error context.

        Returns:
            The open connection.

        Raises:
            SagaError: If the store has been closed.
        """
        if self._connection is None:
            raise SagaError(MSG_CLOSED, operation=f"saga_log.{operation}")
        return self._connection

    def _initialize_schema(self) -> None:
        """Provision or migrate the schema through :func:`apply_schema`.

        Raises:
            SagaError: If provisioning fails.
        """
        connection = self._require_connection("__init__")
        try:
            apply_schema(
                connection,
                baseline=SCHEMA_STATEMENTS,
                migrations=MIGRATIONS,
                target_version=SCHEMA_VERSION,
            )
        except StorageError as error:
            raise SagaError(
                MSG_SCHEMA_FAILED,
                operation="saga_log.apply_schema",
                file_path=None if self.path == MEMORY_PATH else self.path,
                details=error.context.details,
            ) from error


def _resolve_path(path: Path | str | None) -> Path | str:
    """Normalize the constructor argument to a database location.

    Args:
        path: User-supplied path, ``":memory:"``, or ``None`` for the default.

    Returns:
        ``":memory:"`` or a :class:`Path`; the default's parent is created.
    """
    if path == MEMORY_PATH:
        return MEMORY_PATH
    if path is None:
        default = user_data_dir() / DATABASE_NAME
        default.parent.mkdir(parents=True, exist_ok=True)
        return default
    return Path(path)


def _open(path: Path | str) -> sqlite3.Connection:
    """Open the connection and apply the session pragmas.

    Args:
        path: Database location.

    Returns:
        Connection in autocommit mode with ``sqlite3.Row`` rows,
        ``foreign_keys=ON`` and (for files) ``journal_mode=WAL``.

    Raises:
        SagaError: If the database cannot be opened.
    """
    try:
        connection = sqlite3.connect(
            path if path == MEMORY_PATH else str(path),
            timeout=CONNECT_TIMEOUT,
            isolation_level=None,
        )
    except sqlite3.Error as error:
        raise SagaError(
            MSG_OPEN_FAILED,
            operation="saga_log.__init__",
            file_path=None if path == MEMORY_PATH else path,
        ) from error
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        if path != MEMORY_PATH:
            connection.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.Error as error:
        connection.close()
        raise _wrap_sqlite_error(error, operation="__init__") from error
    return connection


def _wrap_sqlite_error(error: sqlite3.Error, *, operation: str) -> SagaError:
    """Map a :mod:`sqlite3` failure onto the :class:`SagaError` contract.

    Args:
        error: The underlying failure.
        operation: Calling method name, used for error context.

    Returns:
        ``MSG_LOCKED`` for lock contention, ``MSG_WRITE_FAILED`` otherwise.
    """
    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error):
        return SagaError(MSG_LOCKED, operation=f"saga_log.{operation}")
    return SagaError(MSG_WRITE_FAILED, operation=f"saga_log.{operation}")


__all__ = [
    "CONNECT_TIMEOUT",
    "DATABASE_NAME",
    "MEMORY_PATH",
    "MSG_CLOSED",
    "MSG_LOCKED",
    "MSG_OPEN_FAILED",
    "MSG_QUERY_FAILED",
    "MSG_SCHEMA_FAILED",
    "MSG_WRITE_FAILED",
    "SagaStore",
]
