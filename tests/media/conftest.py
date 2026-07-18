"""Shared fixtures for media processor tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the directory holding committed media fixtures.

    Returns:
        Absolute path to ``tests/media/fixtures``.
    """
    return FIXTURE_DIR
