"""Tests for the shared EXIF, GPS, IPTC, ISO 8601 and QuickTime date parsers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from assertpy import assert_that

from winnow.media._dates import (
    ClockTime,
    SubSecond,
    parse_exif_date_value,
    parse_exif_datetime,
    parse_exif_datetime_value,
    parse_exif_offset,
    parse_exif_subsec,
    parse_gps_datetime_value,
    parse_gps_time,
    parse_iptc_time,
    parse_iptc_value,
    parse_iso8601_value,
    parse_iso_datetime,
    parse_quicktime_seconds,
)
from winnow.models.evidence import ParseStatus, Precision, TimeValue


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


def _rendered(value: TimeValue | ParseStatus) -> str:
    """Render a parse result for comparison.

    Args:
        value: A parsed value or a status.

    Returns:
        The value's rendering, or the status's name.
    """
    return value.render() if isinstance(value, TimeValue) else str(value)


@pytest.mark.parametrize(
    ("raw", "subsec", "offset", "expected"),
    [
        ("2019:07:04 10:10:10", "123", "+09:00", "2019-07-04T10:10:10.123+09:00"),
        ("2019:07:04 10:10:10", "5", None, "2019-07-04T10:10:10.5"),
        ("2019:07:04 10:10:10\x00", None, None, "2019-07-04T10:10:10"),
        ("2019:07:04 10:10:10  ", None, None, "2019-07-04T10:10:10"),
        ("2019:07:04 10:10:10", "12a", "+25:00", "2019-07-04T10:10:10"),
        ("2019:07:04 10:10:10", "  ", "   :  ", "2019-07-04T10:10:10"),
        ("2019:07:04 10:10:  ", "123", "-03:30", "2019-07-04T10:10-03:30"),
        ("2019:07:04   :  :  ", "123", "+09:00", "2019-07-04"),
        ("2019:07:     :  :  ", None, None, "2019-07"),
        ("2019:  :     :  :  ", None, None, "2019"),
        ("2019:07:04 10:  :  ", None, None, "unsupported"),
        ("    :07:04 10:10:10", None, None, "unsupported"),
        ("0000:00:00 00:00:00", None, None, "placeholder"),
        ("0000:00:00 10:10:10", None, None, "placeholder"),
        ("    :  :     :  :  ", None, None, "placeholder"),
        (" " * 19, None, None, "placeholder"),
        ("", None, None, "empty"),
        ("\x00\x00", None, None, "empty"),
        ("2019:00:00 00:00:00", None, None, "malformed"),
        ("2019:02:30 00:00:00", None, None, "malformed"),
        ("2019:07:04 24:00:00", None, None, "malformed"),
        ("2019-07-04T10:10:10", None, None, "malformed"),
        ("2019:07:04", None, None, "malformed"),
        ("2019:07:04 10:10:10 PM", None, None, "malformed"),
        ("20 9:07:04 10:10:10", None, None, "malformed"),
    ],
    ids=[
        "offset_and_subsec",
        "one_subsec_digit",
        "nul_padding",
        "space_padding",
        "malformed_companions_ignored",
        "blank_companions_absent",
        "minute_precision_takes_offset_only",
        "date_only_takes_no_companion",
        "month_only",
        "year_only",
        "hour_only",
        "unknown_leading_component",
        "zeros",
        "zero_date",
        "blanks",
        "all_spaces",
        "empty",
        "nul_only",
        "zero_month",
        "invalid_day",
        "hour_24",
        "iso_text",
        "truncated",
        "trailing_text",
        "mixed_blank_digits",
    ],
)
def test_parse_exif_datetime_value(
    raw: str,
    subsec: str | None,
    offset: str | None,
    expected: str,
) -> None:
    """EXIF dates keep exactly the components and companions the text gives."""
    assert_that(
        _rendered(parse_exif_datetime_value(raw, subsec=subsec, offset=offset)),
    ).is_equal_to(expected)


def test_parse_exif_datetime_value_counts_digits_past_microseconds() -> None:
    """Sub-second digits past six are counted but not kept in the wall time."""
    value = parse_exif_datetime_value("2019:07:04 10:10:10", subsec="12345678")

    assert_that(value).is_equal_to(
        TimeValue(
            wall=datetime(2019, 7, 4, 10, 10, 10, 123456),
            offset_minutes=None,
            offset_origin=None,
            precision=Precision.SUBSECOND,
            fraction_digits=8,
        ),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123", SubSecond(microsecond=123000, digits=3)),
        ("0\x00", SubSecond(microsecond=0, digits=1)),
        ("", ParseStatus.EMPTY),
        ("  ", ParseStatus.EMPTY),
        ("1.5", ParseStatus.MALFORMED),
        ("\u0661\u0662", ParseStatus.MALFORMED),
    ],
    ids=["digits", "padded_zero", "empty", "blank", "decimal_point", "arabic_digits"],
)
def test_parse_exif_subsec(raw: str, expected: SubSecond | ParseStatus) -> None:
    """Sub-seconds are ASCII digits of a decimal fraction."""
    assert_that(parse_exif_subsec(raw)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+09:00", 540),
        ("-03:30", -210),
        ("+14:00", 840),
        ("-12:00", -720),
        ("   :  ", ParseStatus.EMPTY),
        ("", ParseStatus.EMPTY),
        ("+09:60", ParseStatus.MALFORMED),
        ("+15:00", ParseStatus.MALFORMED),
        ("-13:00", ParseStatus.MALFORMED),
        ("+0900", ParseStatus.MALFORMED),
        ("Z", ParseStatus.MALFORMED),
        ("::", ParseStatus.MALFORMED),
    ],
    ids=[
        "east",
        "west_half_hour",
        "max",
        "min",
        "blank",
        "empty",
        "minute_60",
        "above_max",
        "below_min",
        "no_colon",
        "zulu",
        "colons_only",
    ],
)
def test_parse_exif_offset(raw: str, expected: int | ParseStatus) -> None:
    """EXIF offsets are ``±HH:MM`` within the model's range."""
    assert_that(parse_exif_offset(raw)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2019:07:04", "2019-07-04"),
        ("2019:07:  ", "2019-07"),
        ("0000:00:00", "placeholder"),
        ("2019:13:04", "malformed"),
        ("2019-07-04", "malformed"),
    ],
    ids=["day", "month", "zeros", "month_13", "dashes"],
)
def test_parse_exif_date_value(raw: str, expected: str) -> None:
    """An EXIF date alone parses with the same blank and zero rules."""
    assert_that(_rendered(parse_exif_date_value(raw))).is_equal_to(expected)


@pytest.mark.parametrize(
    ("rationals", "expected"),
    [
        ([(10, 1), (10, 1), (12, 1)], ClockTime(10, 10, 12)),
        ([(10, 1), (10, 1), (617, 50)], ClockTime(10, 10, 12, 340000, 2)),
        ([(10, 1), (10, 1), (1, 128)], ClockTime(10, 10, 0, 7812, 7)),
        ([(20, 2), (0, 1), (0, 1)], ClockTime(10, 0, 0)),
        ([(10, 1), (10, 0), (12, 1)], ParseStatus.MALFORMED),
        ([(10, 1), (10, 1)], ParseStatus.MALFORMED),
        ([(-1, 1), (10, 1), (12, 1)], ParseStatus.MALFORMED),
        ([(24, 1), (0, 1), (0, 1)], ParseStatus.MALFORMED),
        ([(10, 1), (10, 1), (60, 1)], ParseStatus.MALFORMED),
        ([(10, 1), (21, 2), (0, 1)], ParseStatus.UNSUPPORTED),
        ([(10, 1), (10, 1), (1, 3)], ParseStatus.UNSUPPORTED),
    ],
    ids=[
        "whole",
        "hundredths",
        "binary_fraction",
        "unreduced_hour",
        "zero_denominator",
        "two_components",
        "negative",
        "hour_24",
        "second_60",
        "fractional_minute",
        "no_decimal_form",
    ],
)
def test_parse_gps_time(
    rationals: list[tuple[int, int]],
    expected: ClockTime | ParseStatus,
) -> None:
    """GPS times are three rationals; fractions keep their exact digits."""
    assert_that(parse_gps_time(rationals)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("date", "time", "expected"),
    [
        ("2019:07:04", [(1, 1), (10, 1), (12, 1)], "2019-07-04T01:10:12+00:00"),
        (None, [(1, 1), (10, 1), (12, 1)], "empty"),
        ("2019:07:04", None, "empty"),
        ("2019:07:  ", [(1, 1), (10, 1), (12, 1)], "unsupported"),
        ("2019:02:30", [(1, 1), (10, 1), (12, 1)], "malformed"),
        ("2019:07:04", [(1, 1), (10, 0), (12, 1)], "malformed"),
    ],
    ids=["both", "no_date", "no_time", "partial_date", "bad_date", "bad_time"],
)
def test_parse_gps_datetime_value(
    date: str | None,
    time: list[tuple[int, int]] | None,
    expected: str,
) -> None:
    """GPS date and time combine into one UTC value only when both parse."""
    value = parse_gps_datetime_value(date, time)

    assert_that(_rendered(value)).is_equal_to(expected)
    if isinstance(value, TimeValue):
        assert_that(value.offset_origin).is_equal_to("utc-by-spec")


@pytest.mark.parametrize(
    ("raw", "expected", "digits"),
    [
        ("2019-07-04T10:10:10+09:00", "2019-07-04T10:10:10+09:00", None),
        ("2019-07-04T10:10:10+0900", "2019-07-04T10:10:10+09:00", None),
        ("2019-07-04T01:10:10.000000Z", "2019-07-04T01:10:10.000000+00:00", 6),
        ("2019-07-04T10:10:10,5", "2019-07-04T10:10:10.5", 1),
        ("2019-07-04T10:10:10.123456789Z", "2019-07-04T10:10:10.123456+00:00", 9),
        ("2019-07-04T10:10:10-00:00", "2019-07-04T10:10:10", None),
        ("2019-07-04T10:10", "2019-07-04T10:10", None),
        ("2019-07-04T10:10-05:00", "2019-07-04T10:10-05:00", None),
        ("2019-07-04", "2019-07-04", None),
        ("2019-07", "2019-07", None),
        ("1985", "1985", None),
        (" 2019-07-04T10:10:10\x00", "2019-07-04T10:10:10", None),
        ("0000-00-00T00:00:00Z", "placeholder", None),
        ("0000", "placeholder", None),
        ("0000-00-00T00:00:00." + "0" * 5000, "placeholder", None),
        ("2016-12-31T23:59:60Z", "malformed", None),
        ("", "empty", None),
        ("2019-07-04T10:10:10Z;2019-07-04T01:10:10Z", "malformed", None),
        ("2019-07-04 10:10:10", "malformed", None),
        ("2019-07-04+09:00", "malformed", None),
        ("2019-07-04T10", "malformed", None),
        ("2019-07-04T10:10:10+15:00", "malformed", None),
        ("2019-02-30", "malformed", None),
        ("2019-07-04T24:00:00", "malformed", None),
        ("20190704T101010", "malformed", None),
    ],
    ids=[
        "colon_offset",
        "basic_offset",
        "zulu_fraction",
        "comma_fraction",
        "nine_digits",
        "unknown_offset",
        "minute",
        "minute_offset",
        "date",
        "month",
        "year",
        "padding",
        "zeros",
        "zero_year",
        "zero_fraction_beyond_int_limit",
        "leap_second_unrepresentable",
        "empty",
        "joined",
        "space_separator",
        "date_with_offset",
        "hour_only",
        "offset_out_of_range",
        "invalid_day",
        "hour_24",
        "basic_format",
    ],
)
def test_parse_iso8601_value(raw: str, expected: str, digits: int | None) -> None:
    """ISO 8601 values keep their stated precision, fraction digits and offset."""
    value = parse_iso8601_value(raw)

    assert_that(_rendered(value)).is_equal_to(expected)
    if isinstance(value, TimeValue):
        assert_that(value.fraction_digits).is_equal_to(digits)


@pytest.mark.parametrize(
    ("seconds", "epoch", "expected"),
    [
        (1, 1904, "1904-01-01T00:00:01+00:00"),
        (2_082_844_800, 1904, "1970-01-01T00:00:00+00:00"),
        (1_562_202_610, 1970, "2019-07-04T01:10:10+00:00"),
        (0, 1904, "empty"),
        (-1, 1904, "malformed"),
        (10**15, 1904, "malformed"),
        (1, 0, "malformed"),
    ],
    ids=["one", "unix_epoch", "unix_count", "zero", "negative", "overflow", "epoch_0"],
)
def test_parse_quicktime_seconds(seconds: int, epoch: int, expected: str) -> None:
    """QuickTime counts are UTC by specification; zero is not a date."""
    value = parse_quicktime_seconds(seconds, epoch=epoch)

    assert_that(_rendered(value)).is_equal_to(expected)
    if isinstance(value, TimeValue):
        assert_that(value.offset_origin).is_equal_to("utc-by-spec")


@pytest.mark.parametrize(
    ("date", "time", "expected"),
    [
        ("20190704", "101010+0900", "2019-07-04T10:10:10+09:00"),
        ("20190704", "101010-0330", "2019-07-04T10:10:10-03:30"),
        ("20190704", "101010", "2019-07-04T10:10:10"),
        ("20190704", None, "2019-07-04"),
        ("20190704", "1010", "2019-07-04"),
        ("20190704", "", "2019-07-04"),
        ("20190700", "101010+0900", "2019-07"),
        ("20190000", None, "2019"),
        ("00000000", None, "placeholder"),
        ("", None, "empty"),
        ("20190007", None, "malformed"),
        ("20191304", None, "malformed"),
        ("2019-07-04", None, "malformed"),
    ],
    ids=[
        "offset_east",
        "offset_west",
        "no_offset",
        "date_only",
        "malformed_time_keeps_date",
        "empty_time_keeps_date",
        "unknown_day_takes_no_time",
        "unknown_month",
        "zeros",
        "empty",
        "day_without_month",
        "month_13",
        "dashes",
    ],
)
def test_parse_iptc_value(date: str, time: str | None, expected: str) -> None:
    """IPTC dates keep IIM's partial precision; a bad time leaves the date."""
    assert_that(_rendered(parse_iptc_value(date, time))).is_equal_to(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("101010+0900", ClockTime(10, 10, 10, offset_minutes=540)),
        ("101010", ClockTime(10, 10, 10)),
        ("", ParseStatus.EMPTY),
        ("101060", ParseStatus.MALFORMED),
        ("241010", ParseStatus.MALFORMED),
        ("101010+0960", ParseStatus.MALFORMED),
        ("101010+1500", ParseStatus.MALFORMED),
        ("10:10:10", ParseStatus.MALFORMED),
    ],
    ids=[
        "offset",
        "no_offset",
        "empty",
        "second_60",
        "hour_24",
        "offset_minute_60",
        "offset_out_of_range",
        "colons",
    ],
)
def test_parse_iptc_time(raw: str, expected: ClockTime | ParseStatus) -> None:
    """IPTC times are ``HHMMSS`` with an optional ``±HHMM``."""
    assert_that(parse_iptc_time(raw)).is_equal_to(expected)
