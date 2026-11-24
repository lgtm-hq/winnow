"""Timestamp parsing helpers shared by the media metadata processors.

EXIF stores capture times as naive ``"YYYY:MM:DD HH:MM:SS"`` strings with no
timezone; ffprobe reports container creation times as ISO 8601 strings that
are usually UTC (``Z`` suffix). :func:`parse_exif_datetime` and
:func:`parse_iso_datetime` map missing, placeholder, or malformed values to
``None`` instead of raising; the existing extractors use them.

The evidence readers (#395, #402) use the ``*_value`` parsers instead. Each
returns a :class:`~winnow.models.evidence.TimeValue` holding exactly what the
text states, or the :class:`~winnow.models.evidence.ParseStatus` that says
why it gave none:

- the precision the text gives (year, month, day, minute, second or a
  fraction of a second, with the number of fraction digits written);
- an offset only when the text states one, never the host's zone, and never
  UTC for a wall time that names no zone;
- no missing component filled in: a value that cannot be represented at a
  precision the model has (an hour without minutes, unknown leading
  components) is ``UNSUPPORTED``.

None of these functions reads a file or the clock.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Final, NamedTuple

from winnow.models.evidence import (
    MAX_OFFSET_MINUTES,
    MIN_OFFSET_MINUTES,
    OffsetOrigin,
    ParseStatus,
    Precision,
    TimeValue,
)

_EXIF_DATETIME_FORMAT: Final[str] = "%Y:%m:%d %H:%M:%S"
_UTC_SUFFIX: Final[str] = "Z"
_UTC_OFFSET: Final[str] = "+00:00"
_EXIF_PADDING: Final[str] = " \t\r\n\x00"

_EXIF_DATETIME_LAYOUT: Final[re.Pattern[str]] = re.compile(
    r"([0-9 ]{4}):([0-9 ]{2}):([0-9 ]{2}) ([0-9 ]{2}):([0-9 ]{2}):([0-9 ]{2})",
)
"""EXIF ``DateTime*``: each component digits, or spaces when unknown."""

_EXIF_DATE_LAYOUT: Final[re.Pattern[str]] = re.compile(
    r"([0-9 ]{4}):([0-9 ]{2}):([0-9 ]{2})",
)
"""EXIF ``GPSDateStamp``: ``YYYY:MM:DD``."""

_EXIF_OFFSET: Final[re.Pattern[str]] = re.compile(r"([+-])([0-9]{2}):([0-9]{2})")
"""EXIF ``OffsetTime*``: ``±HH:MM``."""

_ISO8601: Final[re.Pattern[str]] = re.compile(
    r"(?P<year>[0-9]{4})"
    r"(?:-(?P<month>[0-9]{2})"
    r"(?:-(?P<day>[0-9]{2})"
    r"(?:T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2})(?:[.,](?P<fraction>[0-9]+))?)?"
    r"(?P<zone>Z|[+-][0-9]{2}:?[0-9]{2})?"
    r")?)?)?",
)
"""ISO 8601 extended form, from a year alone to fractional seconds."""

_IPTC_DATE: Final[re.Pattern[str]] = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})")
"""IPTC IIM 2:55 ``DateCreated``: ``CCYYMMDD``."""

_IPTC_TIME: Final[re.Pattern[str]] = re.compile(
    r"([0-9]{2})([0-9]{2})([0-9]{2})(?:([+-])([0-9]{2})([0-9]{2}))?",
)
"""IPTC IIM 2:60 ``TimeCreated``: ``HHMMSS±HHMM``."""

_UNKNOWN_LOCAL_OFFSETS: Final[frozenset[str]] = frozenset({"-00:00", "-0000"})
"""RFC 3339's "offset unknown"; never read as UTC."""

_PRECISION_BY_FIELDS: Final[dict[int, Precision]] = {
    1: Precision.YEAR,
    2: Precision.MONTH,
    3: Precision.DAY,
    5: Precision.MINUTE,
    6: Precision.SECOND,
}
"""Precision of a value stating its first ``n`` of year..second; four
fields (an hour alone) has no precision in the model."""

_ANCHORS: Final[tuple[int, ...]] = (1, 1, 1, 0, 0, 0)
"""Anchor of each component the source does not state."""

_DATE_FIELDS: Final[int] = 3
_DATETIME_FIELDS: Final[int] = 6
_MINUTE_FIELDS: Final[int] = 5
_MICROSECOND_DIGITS: Final[int] = 6
_GPS_COMPONENTS: Final[int] = 3
_HOURS_PER_DAY: Final[int] = 24
_MINUTES_PER_HOUR: Final[int] = 60


class SubSecond(NamedTuple):
    """A parsed EXIF ``SubSecTime*`` value.

    Args:
        microsecond: The fraction in microseconds, truncated past six digits.
        digits: How many fraction digits the source wrote.
    """

    microsecond: int
    digits: int


class ClockTime(NamedTuple):
    """A time of day as one field states it.

    Args:
        hour: Hour, 0 to 23.
        minute: Minute, 0 to 59.
        second: Whole seconds, 0 to 59.
        microsecond: The fraction in microseconds, truncated past six digits.
        fraction_digits: Fraction digits the source gives, or ``None`` for
            whole seconds.
        offset_minutes: UTC offset the field states, or ``None``.
    """

    hour: int
    minute: int
    second: int
    microsecond: int = 0
    fraction_digits: int | None = None
    offset_minutes: int | None = None


def parse_exif_datetime(value: str) -> datetime | None:
    """Parse an EXIF ``DateTime``-style string into a naive datetime.

    EXIF timestamps carry no timezone, and no timezone is guessed here; the
    result is naive and consumers decide how to interpret it. The EXIF
    all-zero placeholder (``"0000:00:00 00:00:00"``) is rejected because it
    has no valid calendar date. Surrounding whitespace and the NUL padding some
    cameras emit are ignored.

    Args:
        value: Raw EXIF tag value such as ``"2024:03:01 12:34:56"``.

    Returns:
        Naive datetime, or ``None`` when the value is empty, a placeholder, or
        not in ``YYYY:MM:DD HH:MM:SS`` format.
    """
    try:
        return datetime.strptime(value.strip(_EXIF_PADDING), _EXIF_DATETIME_FORMAT)
    except ValueError:
        return None


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp such as ffprobe's ``creation_time``.

    A trailing ``Z`` is normalised to ``+00:00`` so the result is timezone
    aware when the source carries an offset. Values without an offset parse
    to a naive datetime.

    Args:
        value: ISO 8601 timestamp, for example ``"2024-03-01T12:34:56.000000Z"``.

    Returns:
        Parsed datetime, or ``None`` when the value cannot be parsed.
    """
    normalised = value.strip()
    if normalised.endswith(_UTC_SUFFIX):
        normalised = normalised[: -len(_UTC_SUFFIX)] + _UTC_OFFSET
    try:
        return datetime.fromisoformat(normalised)
    except ValueError:
        return None


def parse_exif_datetime_value(
    raw: str,
    *,
    subsec: str | None = None,
    offset: str | None = None,
) -> TimeValue | ParseStatus:
    """Parse an EXIF ``DateTime``, ``DateTimeOriginal`` or ``DateTimeDigitized``.

    The text is ``YYYY:MM:DD HH:MM:SS``, NUL- and space-padded at the end.
    EXIF writes an unknown component as spaces: a trailing run of unknown
    components gives a partial precision (``2019:07:04   :  :  `` is a day),
    and any other pattern is ``UNSUPPORTED``. A zero date (``0000:00:00``)
    or an all-blank value is ``PLACEHOLDER``. A zero month or day alone is
    ``MALFORMED``: no vendor profile that permits it is known.

    The companions are applied only when valid and only at a precision that
    can carry them: the sub-seconds to a full second, the offset to a minute
    or finer. A missing, blank or malformed companion leaves the base value
    intact; the reader records the companion as its own observation.

    Args:
        raw: The tag's text.
        subsec: The matching ``SubSecTime*`` text, if present.
        offset: The matching ``OffsetTime*`` text, if present.

    Returns:
        The value, or why there is none.
    """
    fields = _layout_fields(raw, _EXIF_DATETIME_LAYOUT)
    if isinstance(fields, ParseStatus):
        return fields
    fraction = None
    if subsec is not None and len(fields) == _DATETIME_FIELDS:
        parsed_subsec = parse_exif_subsec(subsec)
        if isinstance(parsed_subsec, SubSecond):
            fraction = parsed_subsec
    minutes = None
    if offset is not None and len(fields) >= _MINUTE_FIELDS:
        parsed_offset = parse_exif_offset(offset)
        if isinstance(parsed_offset, int):
            minutes = parsed_offset
    return _build(
        fields,
        microsecond=0 if fraction is None else fraction.microsecond,
        fraction_digits=None if fraction is None else fraction.digits,
        offset_minutes=minutes,
        offset_origin=None if minutes is None else "source",
    )


def parse_exif_subsec(raw: str) -> SubSecond | ParseStatus:
    """Parse an EXIF ``SubSecTime*`` value.

    The text is a decimal fraction's digits: ``"123"`` is 0.123 s and
    ``"5"`` is 0.5 s. Digits past the sixth are counted, not kept.

    Args:
        raw: The tag's text.

    Returns:
        The fraction; ``EMPTY`` when blank; ``MALFORMED`` when it holds
        anything but ASCII digits.
    """
    text = raw.strip(_EXIF_PADDING)
    if not text:
        return ParseStatus.EMPTY
    if not (text.isascii() and text.isdigit()):
        return ParseStatus.MALFORMED
    microsecond = int(text[:_MICROSECOND_DIGITS].ljust(_MICROSECOND_DIGITS, "0"))
    return SubSecond(microsecond=microsecond, digits=len(text))


def parse_exif_offset(raw: str) -> int | ParseStatus:
    """Parse an EXIF ``OffsetTime*`` value, ``±HH:MM``.

    Args:
        raw: The tag's text.

    Returns:
        The offset in minutes; ``EMPTY`` for an empty or ``"   :  "`` value,
        which EXIF uses for "unknown"; ``MALFORMED`` for any other form, a
        minute above 59 or an offset outside -12:00..+14:00.
    """
    text = raw.strip(_EXIF_PADDING)
    if text in ("", ":"):
        return ParseStatus.EMPTY
    match = _EXIF_OFFSET.fullmatch(text)
    if match is None:
        return ParseStatus.MALFORMED
    minutes = _offset_minutes(*match.groups())
    return ParseStatus.MALFORMED if minutes is None else minutes


def parse_exif_date_value(raw: str) -> TimeValue | ParseStatus:
    """Parse an EXIF date alone, ``YYYY:MM:DD``, such as ``GPSDateStamp``.

    Blank and zero handling follows :func:`parse_exif_datetime_value`.

    Args:
        raw: The tag's text.

    Returns:
        The value, at most day precision, or why there is none.
    """
    fields = _layout_fields(raw, _EXIF_DATE_LAYOUT)
    if isinstance(fields, ParseStatus):
        return fields
    return _build(fields)


def parse_gps_time(rationals: Sequence[tuple[int, int]]) -> ClockTime | ParseStatus:
    """Parse an EXIF ``GPSTimeStamp``: hour, minute and second as rationals.

    The seconds may carry a fraction; its digits are those of its exact
    decimal form (``1234/100`` and ``617/50`` both give ``.34``).

    Args:
        rationals: The three ``(numerator, denominator)`` pairs.

    Returns:
        The UTC time of day; ``MALFORMED`` for a wrong count, a zero or
        negative denominator, a negative value or a component out of range;
        ``UNSUPPORTED`` for a fractional hour or minute, or seconds with no
        exact decimal form.
    """
    if len(rationals) != _GPS_COMPONENTS:
        return ParseStatus.MALFORMED
    if any(denominator <= 0 or numerator < 0 for numerator, denominator in rationals):
        return ParseStatus.MALFORMED
    hour, minute, second = (Fraction(n, d) for n, d in rationals)
    if hour.denominator != 1 or minute.denominator != 1:
        return ParseStatus.UNSUPPORTED
    whole = int(second)
    fraction = second - whole
    digits = _decimal_digits(fraction.denominator) if fraction else None
    if fraction and digits is None:
        return ParseStatus.UNSUPPORTED
    if not (
        hour < _HOURS_PER_DAY
        and minute < _MINUTES_PER_HOUR
        and whole < _MINUTES_PER_HOUR
    ):
        return ParseStatus.MALFORMED
    return ClockTime(
        hour=int(hour),
        minute=int(minute),
        second=whole,
        microsecond=int(fraction * 10**_MICROSECOND_DIGITS),
        fraction_digits=digits,
    )


def parse_gps_datetime_value(
    date: str | None,
    time: Sequence[tuple[int, int]] | None,
) -> TimeValue | ParseStatus:
    """Combine EXIF ``GPSDateStamp`` and ``GPSTimeStamp`` into one UTC value.

    EXIF states that both are UTC, so the offset is 0 with origin
    ``utc-by-spec``.

    Args:
        date: The ``GPSDateStamp`` text, or ``None`` when absent.
        time: The ``GPSTimeStamp`` rationals, or ``None`` when absent.

    Returns:
        The value; ``EMPTY`` when either part is absent; otherwise the
        status of the first part that does not parse, or ``UNSUPPORTED``
        for a date that is not a full day.
    """
    if date is None or time is None:
        return ParseStatus.EMPTY
    day = parse_exif_date_value(date)
    if isinstance(day, ParseStatus):
        return day
    if day.precision is not Precision.DAY:
        return ParseStatus.UNSUPPORTED
    clock = parse_gps_time(time)
    if isinstance(clock, ParseStatus):
        return clock
    return _with_clock(day, clock, offset_origin="utc-by-spec", offset_minutes=0)


def parse_iso8601_value(raw: str) -> TimeValue | ParseStatus:
    """Parse ISO 8601 extended text, as XMP and ffprobe write it.

    Accepts ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, ``YYYY-MM-DDTHH:MM``,
    ``YYYY-MM-DDTHH:MM:SS`` and fractional seconds (``.`` or ``,``), each
    time form with an optional ``Z``, ``±HH:MM`` or ``±HHMM``. ``-00:00``
    states that the offset is unknown (RFC 3339) and gives none. A
    semicolon-joined list is not ISO 8601 and is ``MALFORMED``; splitting
    it is the caller's job.

    Args:
        raw: The text.

    Returns:
        The value; ``EMPTY`` when blank; ``PLACEHOLDER`` when every digit
        is zero; ``MALFORMED`` for any other form, an invalid calendar date
        or time, or an offset outside -12:00..+14:00.
    """
    text = raw.strip(_EXIF_PADDING)
    if not text:
        return ParseStatus.EMPTY
    match = _ISO8601.fullmatch(text)
    if match is None:
        return ParseStatus.MALFORMED
    parts = match.groupdict()
    names = ("year", "month", "day", "hour", "minute", "second")
    fields = [int(parts[name]) for name in names if parts[name] is not None]
    fraction = parts["fraction"]
    if not any(fields) and not (fraction or "").strip("0"):
        return ParseStatus.PLACEHOLDER
    zone = parts["zone"]
    minutes = _iso_offset(zone)
    if zone is not None and zone not in _UNKNOWN_LOCAL_OFFSETS and minutes is None:
        return ParseStatus.MALFORMED
    return _build(
        fields,
        microsecond=_microseconds(fraction),
        fraction_digits=None if fraction is None else len(fraction),
        offset_minutes=minutes,
        offset_origin=None if minutes is None else "source",
    )


def parse_quicktime_seconds(
    seconds: int, *, epoch: int = 1904
) -> TimeValue | ParseStatus:
    """Parse a QuickTime-style count of seconds since an epoch, in UTC.

    ``mvhd`` and ``mdhd`` count from 1904-01-01T00:00:00 UTC; pass
    ``epoch=1970`` for a Unix count. The field is UTC by specification even
    though some producers wrote local time; the caller records that.

    Args:
        seconds: The stored integer.
        epoch: The year whose first instant is second zero.

    Returns:
        The value at second precision; ``EMPTY`` for 0, which writers use
        for "not set"; ``MALFORMED`` for a negative count, or a count or
        epoch whose date cannot be represented.
    """
    if seconds == 0:
        return ParseStatus.EMPTY
    if seconds < 0:
        return ParseStatus.MALFORMED
    try:
        wall = datetime(epoch, 1, 1) + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return ParseStatus.MALFORMED
    return _build(
        (wall.year, wall.month, wall.day, wall.hour, wall.minute, wall.second),
        offset_minutes=0,
        offset_origin="utc-by-spec",
    )


def parse_iptc_time(raw: str) -> ClockTime | ParseStatus:
    """Parse IPTC IIM 2:60 ``TimeCreated``, ``HHMMSS±HHMM``.

    A time without the offset is accepted as a wall time with no offset.

    Args:
        raw: The dataset's text.

    Returns:
        The time; ``EMPTY`` when blank; ``MALFORMED`` for any other form or
        an out-of-range component or offset.
    """
    text = raw.strip(_EXIF_PADDING)
    if not text:
        return ParseStatus.EMPTY
    match = _IPTC_TIME.fullmatch(text)
    if match is None:
        return ParseStatus.MALFORMED
    hour, minute, second = (int(part) for part in match.groups()[:3])
    sign, offset_hours, offset_minutes = match.groups()[3:]
    minutes = None
    if sign is not None:
        minutes = _offset_minutes(sign, offset_hours, offset_minutes)
        if minutes is None:
            return ParseStatus.MALFORMED
    if not (
        hour < _HOURS_PER_DAY
        and minute < _MINUTES_PER_HOUR
        and second < _MINUTES_PER_HOUR
    ):
        return ParseStatus.MALFORMED
    return ClockTime(hour=hour, minute=minute, second=second, offset_minutes=minutes)


def parse_iptc_value(date: str, time: str | None = None) -> TimeValue | ParseStatus:
    """Parse IPTC IIM 2:55 ``DateCreated`` with an optional 2:60 ``TimeCreated``.

    The date is ``CCYYMMDD``; IIM writes ``00`` for an unknown month or day,
    which gives year or month precision. The time is applied only to a full
    day and only when it parses; otherwise the date stands alone at day
    precision and the reader records the time separately.

    Args:
        date: The ``DateCreated`` text.
        time: The ``TimeCreated`` text, if present.

    Returns:
        The value; ``EMPTY`` when blank; ``PLACEHOLDER`` for ``00000000``;
        ``MALFORMED`` for any other form, an unknown month with a known day,
        or an invalid calendar date.
    """
    fields = _iptc_date_fields(date)
    if isinstance(fields, ParseStatus):
        return fields
    value = _build(fields)
    if isinstance(value, ParseStatus) or len(fields) < _DATE_FIELDS or time is None:
        return value
    clock = parse_iptc_time(time)
    if isinstance(clock, ParseStatus):
        return value
    return _with_clock(
        value,
        clock,
        offset_origin=None if clock.offset_minutes is None else "source",
        offset_minutes=clock.offset_minutes,
    )


def _iptc_date_fields(raw: str) -> list[int] | ParseStatus:
    """Read the components an IPTC ``CCYYMMDD`` date states.

    Args:
        raw: The dataset's text.

    Returns:
        Year, then month and day when known; ``EMPTY`` when blank;
        ``PLACEHOLDER`` for ``00000000``; ``MALFORMED`` for any other form
        or an unknown month with a known day.
    """
    text = raw.strip(_EXIF_PADDING)
    if not text:
        return ParseStatus.EMPTY
    match = _IPTC_DATE.fullmatch(text)
    if match is None:
        return ParseStatus.MALFORMED
    year, month, day = (int(part) for part in match.groups())
    if not (year or month or day):
        return ParseStatus.PLACEHOLDER
    if not month and day:
        return ParseStatus.MALFORMED
    return [year, month, day][: 1 if not month else 2 if not day else 3]


def _layout_fields(raw: str, layout: re.Pattern[str]) -> list[int] | ParseStatus:
    """Read an EXIF fixed-layout value whose unknown components are blank.

    Args:
        raw: The tag's text; NULs and spaces after the layout are padding.
        layout: The fixed layout, one group per component.

    Returns:
        The known components, a leading run; ``EMPTY`` when empty or
        only NULs; ``PLACEHOLDER`` when it is all blank, every component
        is blank or the date is zero;
        ``MALFORMED`` when the layout or a component's characters are
        wrong; ``UNSUPPORTED`` when an unknown component precedes a known
        one.
    """
    text = raw.rstrip("\x00")
    if not text.strip(_EXIF_PADDING):
        return ParseStatus.PLACEHOLDER if text else ParseStatus.EMPTY
    match = layout.match(text)
    if match is None or text[match.end() :].strip(_EXIF_PADDING):
        return ParseStatus.MALFORMED
    known: list[int | None] = []
    for part in match.groups():
        if part.isdigit():
            known.append(int(part))
        elif not part.strip():
            known.append(None)
        else:
            return ParseStatus.MALFORMED
    count = next((i for i, part in enumerate(known) if part is None), len(known))
    fields = [part for part in known[:count] if part is not None]
    if any(part is not None for part in known[count:]):
        return ParseStatus.UNSUPPORTED
    if not any(fields[:_DATE_FIELDS]):
        return ParseStatus.PLACEHOLDER
    return fields


def _build(
    fields: Sequence[int],
    *,
    microsecond: int = 0,
    fraction_digits: int | None = None,
    offset_minutes: int | None = None,
    offset_origin: OffsetOrigin | None = None,
) -> TimeValue | ParseStatus:
    """Build a value from its leading components, anchoring the rest.

    Args:
        fields: Year, then as many of month, day, hour, minute and second as
            the source states.
        microsecond: The fraction of a second, when one is stated.
        fraction_digits: Fraction digits stated; makes it subsecond.
        offset_minutes: The offset, when the source states one.
        offset_origin: Where the offset came from.

    Returns:
        The value; ``UNSUPPORTED`` for an hour without minutes;
        ``MALFORMED`` when the components are not a valid date and time or
        the offset cannot apply.
    """
    precision = _PRECISION_BY_FIELDS.get(len(fields))
    if precision is None:
        return ParseStatus.UNSUPPORTED
    if fraction_digits is not None:
        precision = Precision.SUBSECOND
    year, month, day, hour, minute, second = (*fields, *_ANCHORS[len(fields) :])
    try:
        return TimeValue(
            wall=datetime(year, month, day, hour, minute, second, microsecond),
            offset_minutes=offset_minutes,
            offset_origin=offset_origin,
            precision=precision,
            fraction_digits=fraction_digits,
        )
    except ValueError:
        return ParseStatus.MALFORMED


def _with_clock(
    day: TimeValue,
    clock: ClockTime,
    *,
    offset_minutes: int | None,
    offset_origin: OffsetOrigin | None,
) -> TimeValue | ParseStatus:
    """Put a time of day on a day-precision value.

    Args:
        day: The date, at day precision.
        clock: The time of day.
        offset_minutes: The offset to record.
        offset_origin: Where it came from.

    Returns:
        The combined value, or ``MALFORMED`` when it cannot be represented.
    """
    wall = day.wall
    return _build(
        (wall.year, wall.month, wall.day, clock.hour, clock.minute, clock.second),
        microsecond=clock.microsecond,
        fraction_digits=clock.fraction_digits,
        offset_minutes=offset_minutes,
        offset_origin=offset_origin,
    )


def _offset_minutes(sign: str, hours: str, minutes: str) -> int | None:
    """Turn offset parts into minutes, checking the range.

    Args:
        sign: ``+`` or ``-``.
        hours: Two-digit hours.
        minutes: Two-digit minutes.

    Returns:
        The signed offset, or ``None`` for a minute above 59 or an offset
        outside -12:00..+14:00.
    """
    if int(minutes) >= _MINUTES_PER_HOUR:
        return None
    total = int(hours) * _MINUTES_PER_HOUR + int(minutes)
    signed = -total if sign == "-" else total
    if not MIN_OFFSET_MINUTES <= signed <= MAX_OFFSET_MINUTES:
        return None
    return signed


def _iso_offset(zone: str | None) -> int | None:
    """Read an ISO 8601 zone designator.

    Args:
        zone: ``Z``, ``±HH:MM``, ``±HHMM`` or ``None``.

    Returns:
        The offset in minutes, or ``None`` when there is no zone, the zone
        says the offset is unknown, or it is out of range.
    """
    if zone is None or zone in _UNKNOWN_LOCAL_OFFSETS:
        return None
    if zone == _UTC_SUFFIX:
        return 0
    digits = zone[1:].replace(":", "")
    return _offset_minutes(zone[0], digits[:2], digits[2:])


def _microseconds(fraction: str | None) -> int:
    """Turn fraction digits into microseconds, truncating past six.

    Args:
        fraction: The digits after the decimal mark, or ``None``.

    Returns:
        The microseconds.
    """
    if fraction is None:
        return 0
    return int(fraction[:_MICROSECOND_DIGITS].ljust(_MICROSECOND_DIGITS, "0"))


def _decimal_digits(denominator: int) -> int | None:
    """Count the decimal digits of a fraction with this reduced denominator.

    Args:
        denominator: A positive reduced denominator.

    Returns:
        The digits of its exact decimal form, or ``None`` when it has none
        (a factor other than 2 and 5).
    """
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    return max(twos, fives) if denominator == 1 else None
