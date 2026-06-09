"""Tests for package metadata."""

from assertpy import assert_that

import winnow


def test_version_is_semver_string() -> None:
    """Package exposes a non-empty version string."""
    assert_that(winnow.__version__).is_not_empty()
