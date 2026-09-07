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


@pytest.fixture(scope="session")
def dated_images_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the EXIF-dated image fixtures at test time.

    Uses the same generator as ``generate_fixtures.py`` so the committed
    ``dated.*`` files and the test-time copies always agree.

    Returns:
        Directory containing ``dated.jpg`` and, when ``pillow-heif`` can
        encode on this platform, ``dated.heic``.
    """
    from tests.media.fixtures.generate_fixtures import write_dated_images

    directory = tmp_path_factory.mktemp("dated")
    write_dated_images(directory)
    return directory
