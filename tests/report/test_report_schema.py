"""Tests for the report schema contract.

Behavioral coverage of the DDL (provisioning, constraints, triggers, FTS) is
exercised against real SQLite databases in ``tests/report/test_database.py``;
this module only pins the externally visible schema contract values.
"""

from __future__ import annotations

from assertpy import assert_that

from winnow.report.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    TERMINAL_RUN_STATUSES,
    RunStatus,
)


def test_schema_version_is_two() -> None:
    """The current report schema is version 2 (fixed external contract)."""
    assert_that(SCHEMA_VERSION).is_equal_to(2)


def test_every_version_bump_has_a_migration() -> None:
    """Each version after the v2 baseline is reachable by exactly one step."""
    assert_that(SCHEMA_VERSION).is_equal_to(2 + len(MIGRATIONS))
    assert_that([m.version for m in MIGRATIONS]).is_equal_to(
        list(range(3, SCHEMA_VERSION + 1)),
    )


def test_run_status_values() -> None:
    """RunStatus exposes the expected lowercase lifecycle states."""
    assert_that([status.value for status in RunStatus]).is_equal_to(
        ["running", "completed", "failed"],
    )


def test_terminal_statuses_exclude_running() -> None:
    """Only completed and failed mark a run as finished."""
    assert_that(TERMINAL_RUN_STATUSES).contains_only(
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    )
