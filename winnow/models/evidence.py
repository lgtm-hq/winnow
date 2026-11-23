"""Evidence about a file and the decisions made from it (#394).

Every observation a reader makes about a file (a date, a type signal, an
identifier) is an :class:`Observation` kept with its source, exact locator,
raw value and parsed value. One collection over one file is an
:class:`EvidenceSet`. A :class:`Decision` is stored separately and names the
rule that chose, the observations it chose and the ones it rejected.

Rules for every reader that writes observations (ADR "Evidence model"):

- Never drop a value because another one already answered. Duplicates and
  empty, placeholder or malformed values are kept with their
  :class:`ParseStatus`.
- Never convert with the host timezone. :attr:`TimeValue.wall` is the clock
  reading the file records; :attr:`TimeValue.offset_minutes` comes only from
  the file itself or from a specification that states UTC.
- Partial dates keep their precision: ``1985`` is :attr:`Precision.YEAR`,
  never 1 January 1985.
- An observation's :attr:`Observation.id` comes from :func:`observation_id`,
  so reordering a collection cannot change it.
- Paths are stored with :func:`encode_path`, which writes a name that is not
  valid UTF-8 as ``hex:`` bytes, so every file a filesystem allows can be
  stored and hashed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum, auto
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictInt,
    field_validator,
    model_validator,
)

EVIDENCE_SCHEMA_VERSION: Final[int] = 1
"""Schema version of :class:`EvidenceSet` and everything it contains.

Bump on any change to the stored shape or to :func:`observation_id`.
"""

MIN_OFFSET_MINUTES: Final[int] = -12 * 60
"""Smallest UTC offset accepted, ``-12:00``."""

MAX_OFFSET_MINUTES: Final[int] = 14 * 60
"""Largest UTC offset accepted, ``+14:00``."""

_MODEL_CONFIG: Final[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class Claim(StrEnum):
    """What an observation or a decision is about."""

    MEDIA_TYPE = auto()
    """Image, video or audio."""

    CONTAINER = auto()
    """The container or format: ``iso-bmff``, ``jpeg``, an ``ftyp`` brand."""

    CAPTURE_TIME = auto()
    """The moment the picture or recording was taken."""

    DIGITIZED_TIME = auto()
    """When it was converted to digital; the scan time for a scan."""

    MODIFIED_TIME = auto()
    """An edit or a metadata write."""

    RELEASE_TIME = auto()
    """An audio release or original-release date."""

    RECORDING_TIME = auto()
    """A recording date such as ID3 ``TDRC``; not this file's capture."""

    CONTAINER_CREATED_TIME = auto()
    """Muxing, encoding or container creation; not an established capture."""

    UNKNOWN = auto()
    """An unmapped field, kept with its exact locator."""

    FILE_CREATED_TIME = auto()
    """When this copy appeared on this filesystem (birth time)."""

    FILE_MODIFIED_TIME = auto()
    """The filesystem modification time."""

    FILE_CHANGED_TIME = auto()
    """The inode change time (``ctime``)."""

    RECEIVED_TIME = auto()
    """When a messaging app saved or received the file."""

    UPLOADED_TIME = auto()
    """When a cloud service received the file."""

    CONTENT_ID = auto()
    """An identifier such as Apple ``ContentIdentifier`` or ``ImageUniqueID``."""

    DEVICE = auto()
    """The camera or phone make and model."""

    SOFTWARE = auto()
    """The program that wrote the file or its metadata."""

    SCREEN_CAPTURE = auto()
    """An embedded screenshot marker or a recognised screenshot file name."""


class SourceKind(StrEnum):
    """Where an observation was read.

    The first six equal the detection signal sources (``override``,
    ``ftyp``, ``track-handler``, ``signature``, ``puremagic``,
    ``extension``) one to one.
    """

    OVERRIDE = auto()
    FTYP = auto()
    TRACK_HANDLER = auto()
    SIGNATURE = auto()
    PUREMAGIC = auto()
    EXTENSION = auto()
    EXIF = auto()
    XMP = auto()
    IPTC = auto()
    PNG_TEXT = auto()
    QUICKTIME = auto()
    MATROSKA = auto()
    RIFF = auto()
    AUDIO_TAG = auto()
    SIDECAR_XMP = auto()
    SIDECAR_AAE = auto()
    SIDECAR_TAKEOUT = auto()
    FILENAME = auto()
    FILESYSTEM = auto()


class Precision(StrEnum):
    """How much of a :class:`TimeValue` the source states.

    Components finer than the precision are anchors, not data: they must be
    the earliest value (month 1, day 1, ``00:00:00``, and fraction digits
    past :attr:`TimeValue.fraction_digits` zero) and are never rendered or
    compared.
    """

    YEAR = auto()
    MONTH = auto()
    DAY = auto()
    MINUTE = auto()
    SECOND = auto()
    SUBSECOND = auto()
    """A fraction of a second; :attr:`TimeValue.fraction_digits` says how
    many digits the source gave."""


_DATE_ONLY: Final[frozenset[Precision]] = frozenset(
    {Precision.YEAR, Precision.MONTH, Precision.DAY},
)

_MICROSECOND_DIGITS: Final[int] = 6

_WALL_TEXT: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?",
)
"""The only text form of a wall time accepted: a full naive ISO datetime."""

OffsetOrigin = Literal["source", "utc-by-spec", "derived"]
"""Where an offset came from: the file, a UTC-by-specification field, or a
rule that derived it from other evidence in the file (#396 defines which;
never the host, and never a rounded difference of two naive times)."""

HEX_PREFIX: Final[str] = "hex:"
"""Prefix of a byte string written as lowercase hexadecimal."""

StrictCount = Annotated[StrictInt, Field(ge=0)]
"""A non-negative integer; booleans, floats and numeric text are rejected."""


def _require_unicode(value: str) -> str:
    """Reject text that cannot be written as UTF-8.

    Args:
        value: The text.

    Returns:
        The text unchanged.

    Raises:
        ValueError: If it holds a lone surrogate, such as an undecodable
            byte from a file name; such bytes are stored as ``hex:...``.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "text must be valid Unicode; store undecodable bytes as 'hex:...'",
        ) from exc
    return value


def _printable(value: str) -> str:
    """Make free text writable as UTF-8, escaping lone surrogates.

    Args:
        value: The text.

    Returns:
        The text with each lone surrogate written as ``\\udcXX``.
    """
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


Text = Annotated[str, AfterValidator(_require_unicode)]
"""Text stored exactly; it must be valid Unicode."""

Message = Annotated[str, AfterValidator(_printable)]
"""Human-readable text; lone surrogates are escaped, not rejected."""


def encode_path(path: Path) -> str:
    """Encode a path for storage and hashing, reversibly and byte-safely.

    A path whose bytes are valid UTF-8 is stored as that text. A path whose
    bytes are not (possible on Linux), or whose text itself starts with
    ``hex:``, is stored as ``hex:`` and its bytes in lowercase hexadecimal.
    Paths are stored as given: no resolution, case folding or Unicode
    normalisation, so two spellings of one file are two paths.

    Args:
        path: The path.

    Returns:
        The stored text.
    """
    raw = os.fsencode(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"{HEX_PREFIX}{raw.hex()}"
    if text.startswith(HEX_PREFIX):
        return f"{HEX_PREFIX}{raw.hex()}"
    return text


def decode_path(text: str) -> Path:
    """Invert :func:`encode_path`.

    Args:
        text: The stored text.

    Returns:
        The path.

    Raises:
        ValueError: If a ``hex:`` value is not valid hexadecimal.
    """
    if text.startswith(HEX_PREFIX):
        return Path(os.fsdecode(bytes.fromhex(text.removeprefix(HEX_PREFIX))))
    return Path(text)


def _path_from_stored(value: object) -> object:
    """Decode stored path text; pass anything else to pydantic.

    Args:
        value: The input value.

    Returns:
        The decoded path's text for text input (pydantic then builds the
        ``Path``; JSON input must reach it as text), otherwise the value
        unchanged.
    """
    if isinstance(value, str):
        return os.fspath(decode_path(value))
    return value


StoredPath = Annotated[
    Path,
    BeforeValidator(_path_from_stored),
    PlainSerializer(encode_path, return_type=str, when_used="json"),
]
"""A path stored with :func:`encode_path` in JSON; a ``Path`` in Python."""


class TimeValue(BaseModel):
    """A clock reading as the source records it.

    Args:
        wall: The clock reading, naive, never shifted to another zone.
            Components finer than ``precision`` hold their anchors (see
            :class:`Precision`). Text input must be a full
            ``YYYY-MM-DDTHH:MM:SS[.ffffff]``; a date alone is rejected, not
            read as midnight.
        offset_minutes: UTC offset from the source itself, or ``None`` when
            the source states none. Only for minute precision or finer, and
            only an integer.
        offset_origin: Where the offset came from; ``None`` exactly when
            ``offset_minutes`` is ``None``.
        precision: How much of ``wall`` the source states.
        fraction_digits: For :attr:`Precision.SUBSECOND` only, how many
            fraction digits the source gave. ``wall`` keeps at most six; a
            source with more is truncated here, and its observation's
            ``raw`` keeps every digit.
    """

    model_config = _MODEL_CONFIG

    wall: datetime
    offset_minutes: StrictInt | None
    offset_origin: OffsetOrigin | None
    precision: Precision
    fraction_digits: Annotated[StrictInt, Field(ge=1)] | None = None

    @field_validator("wall", mode="before")
    @classmethod
    def _full_wall(cls, value: object) -> object:
        """Accept a datetime or the full text form only.

        Args:
            value: The input.

        Returns:
            The input unchanged.

        Raises:
            ValueError: For a date, a partial or zoned text, or a number.
        """
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and _WALL_TEXT.fullmatch(value):
            return value
        raise ValueError(
            "wall must be a datetime or full 'YYYY-MM-DDTHH:MM:SS[.ffffff]' text",
        )

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        """Reject values that claim more than the source stated.

        Returns:
            The validated value.

        Raises:
            ValueError: If ``wall`` is aware or folded, the offset and its
                origin disagree, the offset is out of range or not allowed
                at this precision, the fraction digits do not fit the
                precision, a component below the precision is not its
                anchor, or the UTC instant is not representable.
        """
        if self.wall.tzinfo is not None:
            raise ValueError("wall must be naive; put the offset in offset_minutes")
        if self.wall.fold:
            raise ValueError("wall must have fold=0; a naive wall has no fold")
        _check_offset(self.offset_minutes, self.offset_origin, self.precision)
        _check_fraction_digits(self.fraction_digits, self.precision)
        _check_anchors(self.wall, self.precision, self.fraction_digits)
        if self.offset_minutes is not None:
            try:
                _to_utc(self.wall, self.offset_minutes)
            except OverflowError as exc:
                raise ValueError(
                    "the UTC instant of this wall time and offset is outside"
                    " the representable range",
                ) from exc
        return self

    def instant(self) -> datetime | None:
        """Return the moment in UTC, when the source establishes one.

        For a minute or a fraction of a second the moment is the start of
        that interval: ``10:10`` at ``+09:00`` gives ``01:10:00`` UTC, the
        first instant of the stated minute. Validation guarantees the
        conversion is representable, so this never raises.

        Returns:
            An aware UTC datetime when ``offset_minutes`` is set, otherwise
            ``None``. A wall reading without an offset is not an instant,
            and date-only values never have an offset.
        """
        if self.offset_minutes is None:
            return None
        return _to_utc(self.wall, self.offset_minutes)

    def render(self) -> str:
        """Render only the components the source states.

        Returns:
            ISO 8601 text with a four-digit year on every platform:
            ``1985``, ``1985-07``, ``1985-07-04``, ``1985-07-04T10:10``,
            ``1985-07-04T10:10:10`` or ``1985-07-04T10:10:10.123`` (as many
            fraction digits as the source gave, at most six), with
            ``+09:00`` appended when an offset is known.
        """
        wall = self.wall
        parts = {
            Precision.YEAR: f"{wall.year:04d}",
            Precision.MONTH: f"-{wall.month:02d}",
            Precision.DAY: f"-{wall.day:02d}",
            Precision.MINUTE: f"T{wall.hour:02d}:{wall.minute:02d}",
            Precision.SECOND: f":{wall.second:02d}",
            Precision.SUBSECOND: f".{wall.microsecond:06d}"[
                : 1 + min(self.fraction_digits or 0, _MICROSECOND_DIGITS)
            ],
        }
        order = list(Precision)
        text = "".join(parts[p] for p in order[: order.index(self.precision) + 1])
        if self.offset_minutes is None:
            return text
        sign = "-" if self.offset_minutes < 0 else "+"
        hours, minutes = divmod(abs(self.offset_minutes), 60)
        return f"{text}{sign}{hours:02d}:{minutes:02d}"


def _to_utc(wall: datetime, offset_minutes: int) -> datetime:
    """Convert a naive wall time at a fixed offset to UTC.

    Args:
        wall: The naive clock reading.
        offset_minutes: Its UTC offset.

    Returns:
        The aware UTC datetime.

    Raises:
        OverflowError: If the result is outside ``datetime``'s range.
    """
    zone = timezone(timedelta(minutes=offset_minutes))
    return wall.replace(tzinfo=zone).astimezone(UTC)


def _check_offset(
    offset_minutes: int | None,
    offset_origin: OffsetOrigin | None,
    precision: Precision,
) -> None:
    """Check an offset against its origin, its range and the precision.

    Args:
        offset_minutes: The offset in minutes, or ``None``.
        offset_origin: Where it came from, or ``None``.
        precision: The value's precision.

    Raises:
        ValueError: If the offset is inconsistent or invalid.
    """
    if offset_minutes is None:
        if offset_origin is not None:
            raise ValueError("offset_origin requires offset_minutes")
        return
    if offset_origin is None:
        raise ValueError("offset_minutes requires offset_origin")
    if not MIN_OFFSET_MINUTES <= offset_minutes <= MAX_OFFSET_MINUTES:
        raise ValueError(
            f"offset_minutes must be within {MIN_OFFSET_MINUTES}.."
            f"{MAX_OFFSET_MINUTES}, got {offset_minutes}",
        )
    if offset_origin == "utc-by-spec" and offset_minutes != 0:
        raise ValueError("a utc-by-spec offset must be 0")
    if precision in _DATE_ONLY:
        raise ValueError(f"a {precision} value has no time of day to offset")


def _check_fraction_digits(fraction_digits: int | None, precision: Precision) -> None:
    """Check that fraction digits are given exactly for subsecond values.

    Args:
        fraction_digits: The source's fraction digit count, or ``None``.
        precision: The value's precision.

    Raises:
        ValueError: If a subsecond value lacks the count or another value
            has one.
    """
    if precision is Precision.SUBSECOND and fraction_digits is None:
        raise ValueError("a subsecond value needs fraction_digits")
    if precision is not Precision.SUBSECOND and fraction_digits is not None:
        raise ValueError(f"a {precision} value has no fraction digits")


def _check_anchors(
    wall: datetime,
    precision: Precision,
    fraction_digits: int | None,
) -> None:
    """Check that components below the precision hold their anchors.

    Args:
        wall: The clock reading.
        precision: How much of it the source states.
        fraction_digits: Fraction digits the source gave, for subseconds.

    Raises:
        ValueError: If a component the source does not state is not its
            anchor, so the stored value would claim more than was read.
    """
    unstated: dict[Precision, tuple[int, ...]] = {
        Precision.YEAR: (wall.month - 1, wall.day - 1),
        Precision.MONTH: (wall.day - 1,),
        Precision.DAY: (),
        Precision.MINUTE: (wall.second,),
        Precision.SECOND: (),
        Precision.SUBSECOND: (),
    }
    time_of_day = (wall.hour, wall.minute, wall.second, wall.microsecond)
    components = unstated[precision]
    if precision in _DATE_ONLY:
        components = (*components, *time_of_day)
    elif precision is not Precision.SUBSECOND:
        components = (*components, wall.microsecond)
    elif fraction_digits is not None and fraction_digits < _MICROSECOND_DIGITS:
        unit = 10 ** (_MICROSECOND_DIGITS - fraction_digits)
        components = (wall.microsecond % unit,)
    if any(components):
        raise ValueError(
            f"a {precision} value must leave finer components at their anchors",
        )


class ParseStatus(StrEnum):
    """How a raw value parsed."""

    OK = auto()
    """Parsed; ``value`` holds the result."""

    EMPTY = auto()
    """The field is present but holds nothing usable, or names no value."""

    PLACEHOLDER = auto()
    """A known filler such as ``0000:00:00 00:00:00``."""

    MALFORMED = auto()
    """The field breaks its format."""

    UNSUPPORTED = auto()
    """Well formed, but in a form winnow does not parse."""


class Observation(BaseModel):
    """One value one reader saw in one place.

    Every text field must be valid Unicode: a reader writes bytes as
    ``"hex:..."`` rather than as undecodable text.

    Args:
        id: Stable identity from :func:`observation_id`.
        claim: What the value is about.
        source: Where it was read.
        locator: The exact field, for example ``"ExifIFD:DateTimeOriginal"``,
            ``"ftyp:major_brand"`` or ``"fs:st_birthtime"``. Repeated
            occurrences of one field get distinct locators through a
            ``#<n>`` occurrence counter scoped to that field, in emitted
            order. XML fields are named by namespace URI and local name,
            never by prefix.
        origin: The file the value came from. Until #402, always the file
            being collected; stored with :func:`encode_path`.
        raw: The value as read: bytes as ``"hex:..."``, rationals as
            ``"n/d"``, joined fields as ``"a | b"``; ``None`` when absent.
        value: The parsed value, or ``None`` when nothing parsed.
        status: How ``raw`` parsed.
        family: Provenance family: values one writer wrote together share it
            (``"exif"``, ``"xmp"``, ``"quicktime-keys"``, ``"fs"``, ...). A
            correlation hint, not proof of independent provenance.
        reader: The program that read it (``"pillow"``, ``"ffprobe"``,
            ``"winnow"``, ...).
        reader_version: That program's version.
    """

    model_config = _MODEL_CONFIG

    id: Text
    claim: Claim
    source: SourceKind
    locator: Text
    origin: StoredPath
    raw: Text | None
    value: TimeValue | Text | None
    status: ParseStatus
    family: Text
    reader: Text
    reader_version: Text


ReadIssueStatus = Literal["backend-unavailable", "unreadable", "failed", "file-changed"]
"""Why a read produced less than it should have."""


class ReadIssue(BaseModel):
    """A reason some evidence is missing or may be stale.

    Args:
        reader: The reader concerned, or ``"winnow"`` for the collector.
        status: What went wrong.
        message: A human-readable detail; undecodable characters are
            escaped.
    """

    model_config = _MODEL_CONFIG

    reader: Text
    status: ReadIssueStatus
    message: Message


FRESHNESS_FIELDS: Final[tuple[str, ...]] = (
    "path",
    "size",
    "mtime_ns",
    "ctime_ns",
    "device",
    "inode",
)
"""The :class:`FileSnapshot` fields compared to decide a file is unchanged."""


class FileSnapshot(BaseModel):
    """A file's ``stat`` identity at one moment.

    Equal snapshots are a freshness hint, never proof the content is
    unchanged; a consumer that needs exact identity reads the file again.

    Args:
        path: The file, as given to the collector; stored with
            :func:`encode_path`.
        size: Size in bytes.
        mtime_ns: Modification time, nanoseconds since the epoch.
        device: ``st_dev``.
        inode: ``st_ino``.
        ctime_ns: Inode change time, nanoseconds since the epoch.
        birthtime_ns: Birth time in nanoseconds, where the platform reports
            it exactly (``st_birthtime_ns``); ``None`` otherwise, including
            where only the float ``st_birthtime`` exists.
    """

    model_config = _MODEL_CONFIG

    path: StoredPath
    size: StrictCount
    mtime_ns: StrictInt
    device: StrictCount
    inode: StrictCount
    ctime_ns: StrictInt
    birthtime_ns: StrictInt | None = None

    @classmethod
    def from_stat(cls, path: Path, result: os.stat_result) -> FileSnapshot:
        """Build a snapshot from a ``stat`` result.

        Args:
            path: The path that was stat'ed (or opened and ``fstat``'ed).
            result: The result.

        Returns:
            The snapshot.
        """
        birthtime: int | None = getattr(result, "st_birthtime_ns", None)
        return cls(
            path=path,
            size=result.st_size,
            mtime_ns=result.st_mtime_ns,
            device=result.st_dev,
            inode=result.st_ino,
            ctime_ns=result.st_ctime_ns,
            birthtime_ns=birthtime,
        )

    def differences(self, other: FileSnapshot) -> tuple[str, ...]:
        """Name the :data:`FRESHNESS_FIELDS` that differ from ``other``.

        Args:
            other: The snapshot to compare with.

        Returns:
            The differing field names, empty when the file looks unchanged.
        """
        return tuple(
            name
            for name in FRESHNESS_FIELDS
            if getattr(self, name) != getattr(other, name)
        )


class EvidenceSet(BaseModel):
    """Everything one collection observed about one file.

    ``EvidenceSet.model_dump_json()`` is the stored and ``--json`` form.

    Args:
        schema_version: :data:`EVIDENCE_SCHEMA_VERSION`; any other version is
            rejected rather than read as this one.
        snapshot: The file's identity before reading; ``None`` only when the
            file could not be stat'ed, and then there are no observations
            and ``issues`` holds an ``unreadable`` issue.
        observations: Every observation, in collection order.
        issues: Reasons evidence is missing or may be stale.
        dependencies: Every other file read, sidecars and ambiguous
            candidates included (#402).
        association_fingerprint: Fingerprint of the directory names and
            sidecar matching rules used (#402).
    """

    model_config = _MODEL_CONFIG

    schema_version: StrictInt = EVIDENCE_SCHEMA_VERSION
    snapshot: FileSnapshot | None
    observations: tuple[Observation, ...]
    issues: tuple[ReadIssue, ...]
    dependencies: tuple[FileSnapshot, ...] = ()
    association_fingerprint: Text | None = None

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        """Reject a schema version this code does not write.

        Args:
            value: The stored version.

        Returns:
            The version.

        Raises:
            ValueError: If it is not :data:`EVIDENCE_SCHEMA_VERSION`.
        """
        if value != EVIDENCE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported evidence schema version {value};"
                f" this code reads {EVIDENCE_SCHEMA_VERSION}",
            )
        return value

    @model_validator(mode="after")
    def _unstat_means_unreadable(self) -> Self:
        """Require an unreadable issue and no observations without a snapshot.

        Returns:
            The validated set.

        Raises:
            ValueError: If ``snapshot`` is ``None`` but observations exist or
                no issue says the file was unreadable.
        """
        if self.snapshot is not None:
            return self
        if self.observations:
            raise ValueError("observations need the snapshot they were read under")
        if not any(issue.status == "unreadable" for issue in self.issues):
            raise ValueError("a set without a snapshot needs an unreadable issue")
        return self


class Certainty(StrEnum):
    """How far a decision can be trusted."""

    CONFIRMED = auto()
    PROBABLE = auto()
    WEAK = auto()
    NONE = auto()


class Rejection(BaseModel):
    """An observation a rule did not choose, and why.

    Args:
        observation: Index into :attr:`EvidenceSet.observations`.
        reason: A stable code: ``"edit-time"``, ``"placeholder"``,
            ``"future"``, ``"lower-rank"``, ``"conflict"``, ...
    """

    model_config = _MODEL_CONFIG

    observation: StrictCount
    reason: Text


class Decision(BaseModel):
    """What a rule decided for one claim, kept apart from the evidence.

    Indices refer to the :class:`EvidenceSet` the decision was made from and
    are local to it; durable references use observation IDs. Checking the
    indices against that set is the job of whatever stores the two together.

    Args:
        claim: What was decided.
        value: The decided value, or ``None`` when the evidence establishes
            none.
        certainty: How far it can be trusted.
        rule_id: The rule that decided.
        rule_version: That rule's version.
        chosen: Observations the value came from.
        rejected: Observations set aside, each with its reason.
        conflicts: Observations that contradict the value.
        warnings: Stable warning codes.
        incomparable: Observations that could not be compared with the
            value, for example a wall time without an offset against an
            instant.
    """

    model_config = _MODEL_CONFIG

    claim: Claim
    value: TimeValue | Text | None
    certainty: Certainty
    rule_id: Text
    rule_version: StrictInt
    chosen: tuple[StrictCount, ...]
    rejected: tuple[Rejection, ...]
    conflicts: tuple[StrictCount, ...]
    warnings: tuple[Text, ...] = ()
    incomparable: tuple[StrictCount, ...] = ()


def snapshot_fingerprint(snapshot: FileSnapshot | None) -> str | None:
    """Fingerprint a snapshot for :func:`observation_id`.

    The fingerprint covers every snapshot field: path (as
    :func:`encode_path` writes it), size, ``mtime_ns``, device, inode,
    ``ctime_ns`` and ``birthtime_ns``.

    Args:
        snapshot: The snapshot, or ``None`` when the file was not stat'ed.

    Returns:
        A SHA-256 hex digest over the snapshot's canonical JSON, or ``None``.
    """
    if snapshot is None:
        return None
    return _sha256(snapshot.model_dump(mode="json"))


def observation_id(observation: Observation, *, snapshot: FileSnapshot | None) -> str:
    """Derive an observation's stable identity.

    The identity is a SHA-256 over a canonical JSON object of the evidence
    schema version, the source snapshot's fingerprint, the origin, the
    claim, the source, the family, the exact locator, the raw value, the
    parsed value, the status and the reader fingerprint (``reader`` and
    ``reader_version``). Two observations share an ID only when they say the
    same thing about the same field occurrence of the same file state. The
    observation's current ``id``, its position and the rest of the
    collection are not part of it.

    Args:
        observation: The observation; its ``id`` is ignored.
        snapshot: The snapshot of the file the value was read from.

    Returns:
        ``"sha256:"`` followed by the hex digest.
    """
    value = observation.value
    payload = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "snapshot": snapshot_fingerprint(snapshot),
        "origin": encode_path(observation.origin),
        "claim": str(observation.claim),
        "source": str(observation.source),
        "family": observation.family,
        "locator": observation.locator,
        "raw": observation.raw,
        "value": (
            value.model_dump(mode="json") if isinstance(value, TimeValue) else value
        ),
        "status": str(observation.status),
        "reader": observation.reader,
        "reader_version": observation.reader_version,
    }
    return f"sha256:{_sha256(payload)}"


def _sha256(payload: object) -> str:
    """Hash a JSON-compatible object in canonical form.

    Every text in the payload is valid Unicode (paths go through
    :func:`encode_path`), so the UTF-8 encoding cannot fail.

    Args:
        payload: Object of JSON types only.

    Returns:
        The SHA-256 hex digest of its sorted, compact, UTF-8 JSON.
    """
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "FRESHNESS_FIELDS",
    "HEX_PREFIX",
    "MAX_OFFSET_MINUTES",
    "MIN_OFFSET_MINUTES",
    "Certainty",
    "Claim",
    "Decision",
    "EvidenceSet",
    "FileSnapshot",
    "Observation",
    "OffsetOrigin",
    "ParseStatus",
    "Precision",
    "ReadIssue",
    "ReadIssueStatus",
    "Rejection",
    "SourceKind",
    "StoredPath",
    "TimeValue",
    "decode_path",
    "encode_path",
    "observation_id",
    "snapshot_fingerprint",
]
