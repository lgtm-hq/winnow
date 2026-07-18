"""Tests for shared CLI rendering helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import click
import pytest
from assertpy import assert_that

from winnow.cli.rendering import (
    console_from_context,
    create_console,
    format_size,
    format_timestamp,
)


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


def test_create_console_can_disable_color() -> None:
    """The console honors the no-color flag."""
    console = create_console(no_color=True)
    assert_that(console.no_color).is_true()


def test_console_from_context_reads_no_color_flag() -> None:
    """A context carrying no_color yields a color-disabled console."""
    ctx = click.Context(click.Command(name="probe"), obj={"no_color": True})
    assert_that(console_from_context(ctx).no_color).is_true()


def test_console_from_context_defaults_when_object_missing() -> None:
    """A context without a dict object yields a color-enabled console."""
    ctx = click.Context(click.Command(name="probe"), obj=None)
    assert_that(console_from_context(ctx).no_color).is_false()
