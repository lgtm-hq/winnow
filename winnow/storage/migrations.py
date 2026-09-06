"""Stepwise, in-code SQLite schema migration runner.

Every winnow SQLite store records the schema versions it has applied in a
``schema_version`` table (one row per version, ``MAX(version)`` is current).
:func:`apply_schema` brings a database from whatever version it holds to the
version the running build expects: a fresh database receives the full
``baseline`` DDL, an older database receives each :class:`Migration` in turn,
and a newer database is rejected. There is no ORM and no downgrade path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from winnow.exceptions import StorageError

OPERATION = "apply_schema"
MSG_NEWER_THAN_BUILD = "schema is newer than this build"
MSG_MIGRATIONS_NOT_ASCENDING = "migration versions must be strictly ascending"
MSG_MIGRATION_GAP = "no migration for schema version"
MSG_MIGRATION_FAILED = "failed to apply schema migration"
MSG_BASELINE_FAILED = "failed to apply schema baseline"
MSG_TRANSACTION_OPEN = "apply_schema requires no open transaction"

_INSERT_VERSION = "INSERT INTO schema_version (version) VALUES (?);"


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema upgrade step.

    Attributes:
        version: The schema version this migration upgrades **to** (``>= 2``).
        statements: DDL/DML executed in order inside a single transaction.
    """

    version: int
    statements: tuple[str, ...]


def read_schema_version(connection: sqlite3.Connection) -> int:
    """Return the highest recorded schema version.

    Args:
        connection: Open SQLite connection.

    Returns:
        ``MAX(version)`` from ``schema_version``, or ``0`` when the table does
        not exist or holds no rows.
    """
    table = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'schema_version';",
    ).fetchone()
    if table is None:
        return 0
    row = connection.execute(
        "SELECT MAX(version) FROM schema_version;",
    ).fetchone()
    version = row[0]
    return int(version) if version is not None else 0


def apply_schema(
    connection: sqlite3.Connection,
    *,
    baseline: Sequence[str],
    migrations: Sequence[Migration],
    target_version: int,
) -> int:
    """Bring a database to ``target_version`` and return the version reached.

    - ``current == 0``: execute ``baseline`` (which must create the
      ``schema_version`` table and the full current schema) and record
      ``target_version`` in one transaction. No migrations run.
    - ``current == target_version``: no-op.
    - ``0 < current < target_version``: for each migration with
      ``current < m.version <= target_version`` in ascending order, execute
      its statements and record ``m.version`` in one transaction per
      migration.
    - ``current > target_version``: raise.

    Migration ordering and coverage are validated before anything executes.
    The connection must not have a transaction open: every step commits or
    rolls back on its own, so an outer transaction would be committed or
    rolled back together with the step.

    Args:
        connection: Open SQLite connection with no transaction in progress.
        baseline: Ordered DDL that provisions the full current schema.
        migrations: Upgrade steps, strictly ascending by ``version``.
        target_version: Schema version this build expects.

    Returns:
        The schema version recorded in the database after the call.

    Raises:
        StorageError: If ``connection`` has a transaction open, if the stored
            version is newer than ``target_version``, if ``migrations`` is not
            strictly ascending or misses a version in
            ``current + 1 .. target_version``, or if a statement fails (the
            failing migration's ``version`` is in ``details``).
    """
    if connection.in_transaction:
        raise StorageError(MSG_TRANSACTION_OPEN, operation=OPERATION)
    _validate_ordering(migrations)
    current = read_schema_version(connection)
    if current == target_version:
        return current
    if current > target_version:
        raise StorageError(
            MSG_NEWER_THAN_BUILD,
            operation=OPERATION,
            details={"found": current, "expected": target_version},
        )
    if current == 0:
        _run_step(
            connection,
            statements=baseline,
            version=target_version,
            message=MSG_BASELINE_FAILED,
        )
        return target_version
    pending = _pending_migrations(
        migrations,
        current=current,
        target_version=target_version,
    )
    for migration in pending:
        _run_step(
            connection,
            statements=migration.statements,
            version=migration.version,
            message=MSG_MIGRATION_FAILED,
        )
    return target_version


def _validate_ordering(migrations: Sequence[Migration]) -> None:
    """Reject migrations whose versions are not strictly ascending.

    Args:
        migrations: Candidate upgrade steps.

    Raises:
        StorageError: If any version is not greater than its predecessor.
    """
    versions = [migration.version for migration in migrations]
    if any(
        later <= earlier for earlier, later in zip(versions, versions[1:], strict=False)
    ):
        raise StorageError(
            MSG_MIGRATIONS_NOT_ASCENDING,
            operation=OPERATION,
            details={"versions": versions},
        )


def _pending_migrations(
    migrations: Sequence[Migration],
    *,
    current: int,
    target_version: int,
) -> list[Migration]:
    """Select the contiguous migrations that take ``current`` to the target.

    Args:
        migrations: All known upgrade steps, strictly ascending.
        current: Version currently recorded in the database.
        target_version: Version to reach.

    Returns:
        Migrations with ``current < version <= target_version``, in order.

    Raises:
        StorageError: If any version in ``current + 1 .. target_version`` has
            no migration.
    """
    by_version = {migration.version: migration for migration in migrations}
    missing = [
        version
        for version in range(current + 1, target_version + 1)
        if version not in by_version
    ]
    if missing:
        raise StorageError(
            MSG_MIGRATION_GAP,
            operation=OPERATION,
            details={"found": current, "expected": target_version, "missing": missing},
        )
    return [by_version[version] for version in range(current + 1, target_version + 1)]


def _run_step(
    connection: sqlite3.Connection,
    *,
    statements: Sequence[str],
    version: int,
    message: str,
) -> None:
    """Execute ``statements`` and record ``version`` in one transaction.

    Args:
        connection: Open SQLite connection.
        statements: Statements to execute in order.
        version: Schema version to insert once every statement succeeded.
        message: Error message used when wrapping a failure.

    The transaction is opened explicitly because :mod:`sqlite3` only begins
    one implicitly before DML; without it a leading DDL statement would
    autocommit and survive a later failure.

    Raises:
        StorageError: If any statement fails; the transaction rolls back and
            no version row is recorded.
    """
    try:
        with connection:
            connection.execute("BEGIN;")
            for statement in statements:
                connection.execute(statement)
            connection.execute(_INSERT_VERSION, (version,))
    except sqlite3.Error as error:
        raise StorageError(
            message,
            operation=OPERATION,
            details={"version": version},
        ) from error


__all__ = ["Migration", "apply_schema", "read_schema_version"]
