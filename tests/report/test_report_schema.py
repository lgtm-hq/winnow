"""Tests for the report schema definitions."""

from __future__ import annotations

from assertpy import assert_that

from winnow.report import schema
from winnow.report.schema import (
    CREATE_INDEXES,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    RunStatus,
)


def test_schema_version_is_two() -> None:
    """The current report schema is version 2 (fixed external contract)."""
    assert_that(SCHEMA_VERSION).is_equal_to(2)


def test_run_status_values() -> None:
    """RunStatus exposes the expected lowercase lifecycle states."""
    assert_that([status.value for status in RunStatus]).is_equal_to(
        ["running", "completed", "failed"],
    )


def test_schema_statements_cover_all_tables() -> None:
    """Schema statements provision every report table and the FTS index."""
    joined = "\n".join(SCHEMA_STATEMENTS)

    assert_that(joined).contains("CREATE TABLE IF NOT EXISTS schema_version")
    assert_that(joined).contains("CREATE TABLE IF NOT EXISTS report_runs")
    assert_that(joined).contains("CREATE TABLE IF NOT EXISTS duplicate_groups")
    assert_that(joined).contains("CREATE TABLE IF NOT EXISTS media_files")
    assert_that(joined).contains("CREATE TABLE IF NOT EXISTS operations")
    assert_that(joined).contains("USING fts5")


def test_schema_statements_include_all_indexes() -> None:
    """Every declared index is part of the ordered schema statements."""
    for statement in CREATE_INDEXES:
        assert_that(SCHEMA_STATEMENTS).contains(statement)


def test_duplicate_groups_created_before_media_files() -> None:
    """Referenced tables must be declared before their referencing tables."""
    statements = list(SCHEMA_STATEMENTS)

    groups_index = statements.index(schema.CREATE_DUPLICATE_GROUPS_TABLE)
    media_index = statements.index(schema.CREATE_MEDIA_FILES_TABLE)

    assert_that(groups_index).is_less_than(media_index)
