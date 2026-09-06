"""Tests for the stepwise SQLite schema migration runner."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from assertpy import assert_that

from winnow.exceptions import StorageError
from winnow.storage import Migration, apply_schema, read_schema_version

BASELINE: tuple[str, ...] = (
    "CREATE TABLE schema_version ("
    "version INTEGER NOT NULL PRIMARY KEY, "
    "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));",
    "CREATE TABLE t (a INTEGER);",
)
BASELINE_VERSION = 2
FAILING_STATEMENT = "SELECT * FROM no_such_table;"

ADD_B = Migration(
    version=3,
    statements=("ALTER TABLE t ADD COLUMN b INTEGER;",),
)
ADD_C = Migration(
    version=4,
    statements=("ALTER TABLE t ADD COLUMN c INTEGER;",),
)
BROKEN_3 = Migration(
    version=3,
    statements=("ALTER TABLE t ADD COLUMN b INTEGER;", FAILING_STATEMENT),
)


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    """Yield an empty in-memory SQLite connection.

    Yields:
        An open connection with no tables.
    """
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def v2_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Provision the baseline schema at version 2.

    Args:
        connection: Empty in-memory connection.

    Returns:
        The same connection, now at schema version 2.
    """
    apply_schema(
        connection,
        baseline=BASELINE,
        migrations=(),
        target_version=BASELINE_VERSION,
    )
    return connection


def _columns(connection: sqlite3.Connection) -> list[str]:
    """Return the column names of the fixture table ``t``.

    Args:
        connection: Open connection.

    Returns:
        Column names in declaration order.
    """
    rows = connection.execute("PRAGMA table_info(t);").fetchall()
    return [row[1] for row in rows]


def _versions(connection: sqlite3.Connection) -> list[int]:
    """Return every recorded schema version in ascending order.

    Args:
        connection: Open connection.

    Returns:
        Recorded versions.
    """
    rows = connection.execute(
        "SELECT version FROM schema_version ORDER BY version;",
    ).fetchall()
    return [row[0] for row in rows]


def test_read_schema_version_without_table_is_zero(
    connection: sqlite3.Connection,
) -> None:
    """A database without a schema_version table reports version 0."""
    assert_that(read_schema_version(connection)).is_equal_to(0)


def test_read_schema_version_with_empty_table_is_zero(
    connection: sqlite3.Connection,
) -> None:
    """A schema_version table with no rows reports version 0."""
    connection.execute("CREATE TABLE schema_version (version INTEGER);")

    assert_that(read_schema_version(connection)).is_equal_to(0)


def test_fresh_database_runs_baseline_only(connection: sqlite3.Connection) -> None:
    """A fresh database receives the baseline and skips every migration."""
    reached = apply_schema(
        connection,
        baseline=BASELINE,
        migrations=(BROKEN_3,),
        target_version=3,
    )

    assert_that(reached).is_equal_to(3)
    assert_that(read_schema_version(connection)).is_equal_to(3)
    assert_that(_versions(connection)).is_equal_to([3])
    assert_that(_columns(connection)).is_equal_to(["a"])


def test_upgrade_applies_each_migration_in_order(
    v2_connection: sqlite3.Connection,
) -> None:
    """A version 2 database upgrades through 3 to 4 and records each step."""
    reached = apply_schema(
        v2_connection,
        baseline=BASELINE,
        migrations=(ADD_B, ADD_C),
        target_version=4,
    )

    assert_that(reached).is_equal_to(4)
    assert_that(read_schema_version(v2_connection)).is_equal_to(4)
    assert_that(_columns(v2_connection)).is_equal_to(["a", "b", "c"])
    assert_that(_versions(v2_connection)).is_equal_to([2, 3, 4])


def test_failed_migration_rolls_back_and_reports_version(
    v2_connection: sqlite3.Connection,
) -> None:
    """A failing statement rolls back the whole migration and names it."""
    with pytest.raises(StorageError) as exc_info:
        apply_schema(
            v2_connection,
            baseline=BASELINE,
            migrations=(BROKEN_3,),
            target_version=3,
        )

    error = exc_info.value
    assert_that(error.context.operation).is_equal_to("apply_schema")
    assert_that(error.context.details["version"]).is_equal_to(3)
    assert_that(error.__cause__).is_instance_of(sqlite3.Error)
    assert_that(read_schema_version(v2_connection)).is_equal_to(2)
    assert_that(_columns(v2_connection)).is_equal_to(["a"])


def test_failed_baseline_rolls_back(connection: sqlite3.Connection) -> None:
    """A failing baseline statement leaves the database unprovisioned."""
    with pytest.raises(StorageError) as exc_info:
        apply_schema(
            connection,
            baseline=(*BASELINE, FAILING_STATEMENT),
            migrations=(),
            target_version=2,
        )

    assert_that(exc_info.value.context.details["version"]).is_equal_to(2)
    assert_that(read_schema_version(connection)).is_equal_to(0)


def test_newer_database_is_rejected(v2_connection: sqlite3.Connection) -> None:
    """A database ahead of the build raises with found/expected details."""
    v2_connection.execute("INSERT INTO schema_version (version) VALUES (5);")
    v2_connection.commit()

    with pytest.raises(StorageError) as exc_info:
        apply_schema(
            v2_connection,
            baseline=BASELINE,
            migrations=(ADD_B, ADD_C),
            target_version=4,
        )

    error = exc_info.value
    assert_that(error.message).contains("newer")
    assert_that(error.context.details).is_equal_to({"found": 5, "expected": 4})
    assert_that(read_schema_version(v2_connection)).is_equal_to(5)


def test_gap_in_migrations_executes_nothing(
    v2_connection: sqlite3.Connection,
) -> None:
    """A missing step is rejected before any migration runs."""
    add_e = Migration(
        version=5,
        statements=("ALTER TABLE t ADD COLUMN e INTEGER;",),
    )

    with pytest.raises(StorageError) as exc_info:
        apply_schema(
            v2_connection,
            baseline=BASELINE,
            migrations=(ADD_B, add_e),
            target_version=5,
        )

    assert_that(exc_info.value.context.details["missing"]).is_equal_to([4])
    assert_that(read_schema_version(v2_connection)).is_equal_to(2)
    assert_that(_columns(v2_connection)).is_equal_to(["a"])


def test_unordered_migrations_are_rejected_before_execution(
    v2_connection: sqlite3.Connection,
) -> None:
    """Migrations that are not strictly ascending are rejected up front."""
    with pytest.raises(StorageError) as exc_info:
        apply_schema(
            v2_connection,
            baseline=BASELINE,
            migrations=(ADD_C, ADD_B),
            target_version=4,
        )

    assert_that(exc_info.value.context.details["versions"]).is_equal_to([4, 3])
    assert_that(_columns(v2_connection)).is_equal_to(["a"])


def test_apply_at_target_is_a_no_op(v2_connection: sqlite3.Connection) -> None:
    """Re-applying at the target version adds no rows and runs nothing."""
    reached = apply_schema(
        v2_connection,
        baseline=BASELINE,
        migrations=(BROKEN_3,),
        target_version=2,
    )

    assert_that(reached).is_equal_to(2)
    assert_that(_versions(v2_connection)).is_equal_to([2])
