"""Tests for package metadata."""

import re

from assertpy import assert_that

import winnow

_SEMVER_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(-[0-9A-Za-z-.]+)?(\+[0-9A-Za-z-.]+)?$",
)


def test_version_is_semver_string() -> None:
    """Package exposes a semver-compatible version string."""
    assert_that(_SEMVER_PATTERN.fullmatch(winnow.__version__)).is_not_none()
