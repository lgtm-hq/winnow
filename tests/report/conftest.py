"""Shared fixtures for report database tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from winnow.report import ReportDatabase


@pytest.fixture
def report_db() -> Iterator[ReportDatabase]:
    """Yield a connected in-memory report database.

    Yields:
        A connected :class:`ReportDatabase` backed by an in-memory database.
    """
    database = ReportDatabase().connect()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def seeded_run(report_db: ReportDatabase) -> int:
    """Create a report run and return its identifier.

    Args:
        report_db: Connected report database fixture.

    Returns:
        The identifier of the seeded report run.
    """
    return report_db.create_run(root_path="/library/photos")
