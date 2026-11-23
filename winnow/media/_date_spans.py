"""Compare time values at their stated precision (#396).

A :class:`~winnow.models.evidence.TimeValue` states a span, not a point:
``2019`` is the whole year, ``10:10:10`` the whole second and ``10:10:10.12``
the hundredth of a second starting there. Two values agree when their spans
overlap, or, with an explicit positive tolerance, when the gap between them
is at most that tolerance. A value with an offset is a span of UTC instants;
a value without one is a span of wall-clock readings in an unknown zone. The
two kinds are never compared with each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, auto
from typing import Final

from winnow.models.evidence import Precision, TimeValue

_MICROSECOND_DIGITS: Final[int] = 6
_MONTHS: Final[int] = 12
_WALL_SLACK: Final[timedelta] = timedelta(hours=14)
"""The largest UTC offset in use: a wall reading up to this far past ``now``
in UTC can still be in the past somewhere."""

_SUSPICIOUS_YEARS: Final[frozenset[int]] = frozenset({1904, 1970, 1980, 2000})
"""Years whose 1 January midnight is a common clock epoch or reset value."""

_TIME_OF_DAY: Final[frozenset[Precision]] = frozenset(
    {Precision.MINUTE, Precision.SECOND, Precision.SUBSECOND},
)

_FIXED_UNITS: Final[dict[Precision, timedelta]] = {
    Precision.DAY: timedelta(days=1),
    Precision.MINUTE: timedelta(minutes=1),
    Precision.SECOND: timedelta(seconds=1),
}


class Agreement(StrEnum):
    """How two time values relate."""

    AGREE = auto()
    DIFFER = auto()
    INCOMPARABLE = auto()


@dataclass(frozen=True, slots=True)
class Span:
    """The half-open span ``[start, end)`` a time value states.

    Args:
        start: First moment of the span: aware UTC for an instant, naive for
            a wall clock.
        end: First moment after the span, in the same kind.
        is_instant: Whether the span is of UTC instants.
    """

    start: datetime
    end: datetime
    is_instant: bool


def span_of(value: TimeValue) -> Span:
    """Return the span a value states at its precision.

    Args:
        value: The value.

    Returns:
        The span: in UTC when the value has an offset, otherwise of naive
        wall readings.
    """
    instant = value.instant()
    start = value.wall if instant is None else instant
    return Span(
        start=start,
        end=_span_end(start, value),
        is_instant=instant is not None,
    )


def _span_end(start: datetime, value: TimeValue) -> datetime:
    """Return the first moment after the span starting at ``start``.

    Args:
        start: The span's first moment.
        value: The value, for its precision and fraction digits.

    Returns:
        The end; ``datetime.max`` (in ``start``'s zone) when the end is
        past the representable range.
    """
    try:
        if value.precision is Precision.YEAR:
            return start.replace(year=start.year + 1)
        if value.precision is Precision.MONTH:
            year, month = divmod(start.month, _MONTHS)
            return start.replace(year=start.year + year, month=month + 1)
        return start + _unit(value)
    except (ValueError, OverflowError):
        return datetime.max.replace(tzinfo=start.tzinfo)


def _unit(value: TimeValue) -> timedelta:
    """Return the length of a fixed-length precision unit.

    Args:
        value: A value of day precision or finer.

    Returns:
        The unit: a day, a minute, a second, or for a subsecond value the
        last fraction digit the source gave (a microsecond when it gave
        more than six digits).
    """
    fixed = _FIXED_UNITS.get(value.precision)
    if fixed is not None:
        return fixed
    digits = min(value.fraction_digits or _MICROSECOND_DIGITS, _MICROSECOND_DIGITS)
    return timedelta(microseconds=10 ** (_MICROSECOND_DIGITS - digits))


def compare(first: Span, second: Span, *, tolerance: timedelta) -> Agreement:
    """Compare two spans.

    Args:
        first: One span.
        second: The other.
        tolerance: Extra gap allowed between non-overlapping spans; zero
            requires overlap.

    Returns:
        ``INCOMPARABLE`` when one is of instants and the other of wall
        readings; ``AGREE`` when they overlap or, for a positive tolerance,
        are at most that far apart; otherwise ``DIFFER``.
    """
    if first.is_instant != second.is_instant:
        return Agreement.INCOMPARABLE
    if first.start < second.end and second.start < first.end:
        return Agreement.AGREE
    gap = max(first.start, second.start) - min(first.end, second.end)
    if tolerance > timedelta(0) and gap <= tolerance:
        return Agreement.AGREE
    return Agreement.DIFFER


def is_future(value: TimeValue, *, now_utc: datetime) -> bool:
    """Tell whether a value starts after ``now``.

    An instant is future when its first moment is after ``now``. A wall
    reading has no known zone, so it is future only when its first reading
    is after ``now`` in UTC plus 14 hours, the largest offset in use. Naive
    and aware datetimes are never ordered against each other. A span that
    contains ``now`` is not future.

    Args:
        value: The value.
        now_utc: The current time, aware UTC.

    Returns:
        Whether the value is certainly later than ``now``.
    """
    instant = value.instant()
    if instant is not None:
        return instant > now_utc
    try:
        limit = now_utc.replace(tzinfo=None) + _WALL_SLACK
    except OverflowError:
        return False
    return value.wall > limit


def is_suspicious_default(value: TimeValue) -> bool:
    """Tell whether a value is midnight on 1 January of a clock epoch.

    Args:
        value: The value.

    Returns:
        Whether it states a time of day, and that time is exactly 00:00:00
        on 1 January 1904, 1970, 1980 or 2000.
    """
    wall = value.wall
    return (
        value.precision in _TIME_OF_DAY
        and wall.year in _SUSPICIOUS_YEARS
        and (wall.month, wall.day, wall.hour, wall.minute) == (1, 1, 0, 0)
        and (wall.second, wall.microsecond) == (0, 0)
    )


def fineness(value: TimeValue) -> tuple[int, int]:
    """Order values by how much they state.

    Args:
        value: The value.

    Returns:
        A key that is larger for a finer precision, and for subseconds for
        more fraction digits.
    """
    return list(Precision).index(value.precision), value.fraction_digits or 0
