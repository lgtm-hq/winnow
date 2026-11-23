"""The date resolver's policy, rule table and stable codes (#396).

Kept apart from :mod:`winnow.media.resolve_date` so the screening and the
selection modules can share them without importing each other. Public names
are re-exported from :mod:`winnow.media.resolve_date`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum, auto
from typing import Final

from winnow.models.evidence import Certainty, OffsetOrigin

RULESET_VERSION: Final[int] = 1
"""Version of the rule order and screening; bump on any change to either."""


@dataclass(frozen=True, slots=True)
class ClockDefault:
    """A documented reset value one camera model writes when its clock is unset.

    A capture observation is rejected as ``placeholder`` only when the file
    names this make and model (in its ``device`` observations), the
    observation's locator is exactly ``locator`` and its raw text is
    exactly ``raw``.

    Args:
        make: The camera make, as the file writes it.
        model: The camera model, as the file writes it.
        locator: The exact locator of the field holding the reset value.
        raw: The exact raw text of the reset value.
    """

    make: str
    model: str
    locator: str
    raw: str


@dataclass(frozen=True, slots=True)
class DatePolicy:
    """Inputs to the resolver that are not evidence.

    Args:
        now: The scan start time, timezone-aware. A value later than this
            is rejected as ``future``. Injected, never read from the clock.
        allow_filename_dates: Whether a camera file-name pattern may date a
            file (owner decision of 2026-11-14: on by default, WEAK).
        gps_tolerance: Extra gap allowed between a GPS fix and the chosen
            value for the fix to count as agreeing. Zero means the two must
            overlap at their stated precision.
        agreement_tolerance: Extra gap allowed between any other two
            values for them to agree. Zero means exact overlap.
        known_clock_defaults: Documented camera reset values.

    Raises:
        ValueError: If ``now`` is naive or a tolerance is negative.
    """

    now: datetime
    allow_filename_dates: bool = True
    gps_tolerance: timedelta = timedelta(0)
    agreement_tolerance: timedelta = timedelta(0)
    known_clock_defaults: tuple[ClockDefault, ...] = ()

    def __post_init__(self) -> None:
        """Check the policy.

        Raises:
            ValueError: If ``now`` is naive or a tolerance is negative.
        """
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if self.gps_tolerance < timedelta(0):
            raise ValueError("gps_tolerance must not be negative")
        if self.agreement_tolerance < timedelta(0):
            raise ValueError("agreement_tolerance must not be negative")

    @property
    def now_utc(self) -> datetime:
        """Return ``now`` in UTC.

        Returns:
            The aware UTC datetime.
        """
        return self.now.astimezone(UTC)


class FieldKind(StrEnum):
    """Which kind of date field an observation is, for rule matching."""

    ORIGINAL = auto()
    """EXIF ``DateTimeOriginal`` or XMP ``exif:DateTimeOriginal``."""

    APPLE_CREATIONDATE = auto()
    """QuickTime ``com.apple.quicktime.creationdate``."""

    DATE_CREATED = auto()
    """IPTC 2:55/2:60 or XMP ``photoshop:DateCreated``."""

    GPS = auto()
    """EXIF GPS date and time."""

    TAKEOUT = auto()
    """Google Takeout ``photoTakenTime``."""

    SIDECAR_XMP = auto()
    """An XMP sidecar's ``DateTimeOriginal`` or ``DateCreated``."""

    PNG_CREATION = auto()
    """PNG text ``Creation Time``."""

    FILENAME = auto()
    """A camera file-name pattern."""

    DIGITIZED = auto()
    """EXIF or XMP digitizing or resource creation time."""


@dataclass(frozen=True, slots=True)
class DateRule:
    """One named rule of the resolver.

    Args:
        order: Position in the issue's rule table; lower fires first.
        rule_id: Stable name recorded in every decision it makes.
        certainty: Certainty before conflicts and caps.
        kind: The field kind the rule reads.
        offsets: The offset origins the rule accepts; ``None`` stands for
            a value without an offset (a wall clock).
        confirmable: Whether independent GPS agreement may raise the result
            to CONFIRMED.
        alone: Whether the rule fires only when every eligible observation
            is of its kind (``gps-only``).
        weak_when_coarse: Whether a value coarser than a day is WEAK.
    """

    order: int
    rule_id: str
    certainty: Certainty
    kind: FieldKind
    offsets: frozenset[OffsetOrigin | None]
    confirmable: bool = False
    alone: bool = False
    weak_when_coarse: bool = False


_SOURCE: Final[frozenset[OffsetOrigin | None]] = frozenset({"source"})
_NAIVE: Final[frozenset[OffsetOrigin | None]] = frozenset({None})
_AS_WRITTEN: Final[frozenset[OffsetOrigin | None]] = frozenset({None, "source"})
_INSTANT: Final[frozenset[OffsetOrigin | None]] = frozenset({"source", "utc-by-spec"})

CAPTURE_RULES: Final[tuple[DateRule, ...]] = (
    DateRule(
        order=1,
        rule_id="exif-original-offset",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.ORIGINAL,
        offsets=_SOURCE,
        confirmable=True,
    ),
    DateRule(
        order=2,
        rule_id="quicktime-creationdate",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.APPLE_CREATIONDATE,
        offsets=_SOURCE,
        confirmable=True,
    ),
    DateRule(
        order=4,
        rule_id="exif-original-wallclock",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.ORIGINAL,
        offsets=_NAIVE,
    ),
    DateRule(
        order=5,
        rule_id="iptc-xmp-date-created",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.DATE_CREATED,
        offsets=_AS_WRITTEN,
        weak_when_coarse=True,
    ),
    DateRule(
        order=7,
        rule_id="gps-only",
        certainty=Certainty.WEAK,
        kind=FieldKind.GPS,
        offsets=_INSTANT,
        alone=True,
    ),
    DateRule(
        order=8,
        rule_id="sidecar-takeout",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.TAKEOUT,
        offsets=_INSTANT,
    ),
    DateRule(
        order=9,
        rule_id="sidecar-xmp",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.SIDECAR_XMP,
        offsets=_AS_WRITTEN,
    ),
    DateRule(
        order=10,
        rule_id="png-creation-time",
        certainty=Certainty.WEAK,
        kind=FieldKind.PNG_CREATION,
        offsets=frozenset({None, "source", "utc-by-spec"}),
    ),
    DateRule(
        order=12,
        rule_id="filename-pattern",
        certainty=Certainty.WEAK,
        kind=FieldKind.FILENAME,
        offsets=_NAIVE,
    ),
)
"""The capture rules in firing order (the issue's table, rows 1 to 12)."""

DIGITIZED_RULES: Final[tuple[DateRule, ...]] = (
    DateRule(
        order=1,
        rule_id="digitized-offset",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.DIGITIZED,
        offsets=_SOURCE,
    ),
    DateRule(
        order=2,
        rule_id="digitized-wallclock",
        certainty=Certainty.PROBABLE,
        kind=FieldKind.DIGITIZED,
        offsets=_NAIVE,
    ),
)
"""The digitized-time rules in firing order."""

UNDATED: Final[str] = "undated"
"""Rule id when no rule fires."""

CONFLICTING_CAPTURE: Final[str] = "conflicting-capture"
"""Rule id when the evidence that would date the file contradicts itself."""

CONFLICTING_DIGITIZED: Final[str] = "conflicting-digitized"
"""Rule id when the digitized evidence contradicts itself."""


class Reason(StrEnum):
    """Stable rejection codes recorded in :class:`~winnow.models.evidence.Rejection`."""

    NOT_CAPTURE = "not-capture"
    """The claim is not a capture time (an edit, copy, arrival, release,
    digitizing, recording or container time)."""

    NOT_DIGITIZED = "not-digitized"
    """The claim is not a digitized time (for the digitized decision)."""

    UNPARSED = "unparsed"
    """The value did not parse (malformed or unsupported)."""

    PLACEHOLDER = "placeholder"
    """Empty, all-zero, or a documented camera reset value."""

    FUTURE = "future"
    """Later than the policy's ``now``."""

    EPOCH_CONTAINER = "epoch-container"
    """A QuickTime creation field read directly as the integer 0 (unset)."""

    NO_RULE = "no-rule"
    """A date of the right claim that no rule in this ruleset reads."""

    OFFSET_NOT_EXPLICIT = "offset-not-explicit"
    """The value's offset origin is not one its rule accepts."""

    FOREIGN_ORIGIN = "foreign-origin"
    """An embedded field whose origin is not the file itself."""

    UNASSOCIATED_SIDECAR = "unassociated-sidecar"
    """A sidecar value whose file is not a recorded association."""

    AMBIGUOUS_SIDECAR = "ambiguous-sidecar"
    """More than one sidecar file of one kind offers a value."""

    FILENAME_DISABLED = "filename-disabled"
    """A file-name date while ``allow_filename_dates`` is off; a hint."""

    LOWER_RANK = "lower-rank"
    """Eligible, but not compared because no value was chosen."""

    GPS_DISAGREES = "gps-disagrees"
    """A GPS fix that does not agree with the chosen value; a GPS fix need
    not be contemporaneous with the shutter, so this is not a conflict."""

    CONTAINER_COMPATIBLE = "container-compatible"
    """Container creation that agrees with the chosen value; a hint."""

    CONTAINER_DIFFERS = "container-differs"
    """Container creation that differs from the chosen value: an encoding
    or edit hint, not a contradicting capture claim."""

    CONTAINER_INCOMPARABLE = "container-incomparable"
    """Container creation that cannot be compared with the chosen value."""


class WarningCode(StrEnum):
    """Stable warning codes recorded in ``Decision.warnings``."""

    ZONE_UNKNOWN = "zone-unknown"
    """The value is a wall clock: the timezone it was read in is unknown."""

    SUSPICIOUS_CLOCK_DEFAULT = "suspicious-clock-default"
    """The value is midnight on 1 January of a common clock epoch (1904,
    1970, 1980, 2000) with no documented reset profile; certainty is capped
    at WEAK."""

    SOFTWARE_RECORDED = "software-recorded"
    """The file names software that wrote it; it may have been edited, so
    the result cannot be CONFIRMED."""

    READ_ISSUES = "read-issues"
    """Some evidence is missing or may be stale; the result cannot be
    CONFIRMED."""


BLOCKS_CONFIRMED: Final[frozenset[WarningCode]] = frozenset(
    {
        WarningCode.SUSPICIOUS_CLOCK_DEFAULT,
        WarningCode.SOFTWARE_RECORDED,
        WarningCode.READ_ISSUES,
    },
)
"""Warnings under which a result is never CONFIRMED."""
