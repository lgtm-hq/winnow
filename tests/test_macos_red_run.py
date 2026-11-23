"""Deliberate macOS-only failure for #375's red run; the next commit removes it."""

from __future__ import annotations

import sys

from assertpy import assert_that


def test_fails_only_on_macos() -> None:
    """Fail on darwin so the macOS job and the all-checks gate must go red."""
    assert_that(sys.platform).is_not_equal_to("darwin")
