"""Tests for shared CLI rendering helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from assertpy import assert_that

from winnow.cli.rendering import format_size, format_timestamp


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1048576, "1.0 MiB"),
        (1073741824, "1.0 GiB"),
        (1125899906842624, "1.0 PiB"),
    ],
    ids=[
        "zero",
        "bytes",
        "one-kib",
        "one-and-half-kib",
        "one-mib",
        "one-gib",
        "one-pib",
    ],
)
def test_format_size_renders_binary_units(size_bytes: int, expected: str) -> None:
    """Byte counts render with the expected binary unit suffix."""
    assert_that(format_size(size_bytes)).is_equal_to(expected)


def test_format_size_rejects_negative_values() -> None:
    """A negative byte count is rejected."""
    with pytest.raises(ValueError, match="non-negative"):
        format_size(-1)


def test_format_timestamp_uses_fixed_layout() -> None:
    """Timestamps render in the fixed year-first layout."""
    moment = datetime(2024, 3, 9, 14, 5, 7, tzinfo=UTC)
    assert_that(format_timestamp(moment)).is_equal_to("2024-03-09 14:05:07")
