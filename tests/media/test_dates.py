"""Tests for the shared EXIF and ISO 8601 timestamp parsers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from assertpy import assert_that

from winnow.media._dates import parse_exif_datetime, parse_iso_datetime


def test_parse_exif_datetime_returns_naive_datetime() -> None:
    """A well-formed EXIF timestamp parses to a naive datetime."""
    parsed = parse_exif_datetime("2024:03:01 12:34:56")

    assert_that(parsed).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


def test_parse_exif_datetime_strips_padding() -> None:
    """Whitespace and NUL padding around an EXIF timestamp are ignored."""
    parsed = parse_exif_datetime("  2024:03:01 12:34:56\x00")

    assert_that(parsed).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


@pytest.mark.parametrize(
    "value",
    [
        "0000:00:00 00:00:00",
        "",
        "garbage",
        "2024-03-01 12:34:56",
        "2024:13:01 00:00:00",
    ],
    ids=["zero_placeholder", "empty", "garbage", "iso_separators", "month_13"],
)
def test_parse_exif_datetime_rejects_invalid(value: str) -> None:
    """Placeholder, empty, and malformed EXIF values yield None."""
    assert_that(parse_exif_datetime(value)).is_none()


def test_parse_iso_datetime_normalises_zulu_suffix() -> None:
    """A trailing Z becomes an aware UTC datetime."""
    parsed = parse_iso_datetime("2024-03-01T12:34:56.000000Z")

    assert_that(parsed).is_equal_to(datetime(2024, 3, 1, 12, 34, 56, tzinfo=UTC))


def test_parse_iso_datetime_keeps_explicit_offset() -> None:
    """An explicit numeric offset is preserved on the result."""
    parsed = parse_iso_datetime("2024-03-01T12:34:56+02:00")

    expected = datetime(2024, 3, 1, 12, 34, 56, tzinfo=timezone(timedelta(hours=2)))
    assert_that(parsed).is_equal_to(expected)


def test_parse_iso_datetime_without_offset_is_naive() -> None:
    """A timestamp with no offset parses to a naive datetime."""
    parsed = parse_iso_datetime("2024-03-01T12:34:56")

    assert_that(parsed).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


@pytest.mark.parametrize(
    "value",
    ["", "garbage", "2024:03:01 12:34:56", "Z"],
    ids=["empty", "garbage", "exif_separators", "bare_zulu"],
)
def test_parse_iso_datetime_rejects_invalid(value: str) -> None:
    """Empty and malformed ISO values yield None."""
    assert_that(parse_iso_datetime(value)).is_none()
