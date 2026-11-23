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
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum, auto
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, NonNegativeInt, model_validator

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
    the earliest value (month 1, day 1, ``00:00:00.000000``) and are never
    rendered or compared.
    """

    YEAR = auto()
    MONTH = auto()
    DAY = auto()
    MINUTE = auto()
    SECOND = auto()
    SUBSECOND = auto()


_DATE_ONLY: Final[frozenset[Precision]] = frozenset(
    {Precision.YEAR, Precision.MONTH, Precision.DAY},
)

OffsetOrigin = Literal["source", "utc-by-spec", "derived"]
"""Where an offset came from: the file, a UTC-by-specification field, or a
rule that derived it (for example from GPS time); never the host."""


class TimeValue(BaseModel):
    """A clock reading as the source records it.

    Args:
        wall: The clock reading, naive, never shifted to another zone.
            Components finer than ``precision`` hold their anchors (see
            :class:`Precision`).
        offset_minutes: UTC offset from the source itself, or ``None`` when
            the source states none. Only for minute precision or finer.
        offset_origin: Where the offset came from; ``None`` exactly when
            ``offset_minutes`` is ``None``.
        precision: How much of ``wall`` the source states.
    """

    model_config = _MODEL_CONFIG

    wall: datetime
    offset_minutes: int | None
    offset_origin: OffsetOrigin | None
    precision: Precision

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        """Reject aware walls, inconsistent offsets and invented components.

        Returns:
            The validated value.

        Raises:
            ValueError: If ``wall`` is aware, the offset and its origin
                disagree, the offset is out of range or not allowed at this
                precision, or a component below the precision is not its
                anchor.
        """
        if self.wall.tzinfo is not None:
            raise ValueError("wall must be naive; put the offset in offset_minutes")
        _check_offset(self.offset_minutes, self.offset_origin, self.precision)
        _check_anchors(self.wall, self.precision)
        return self

    def instant(self) -> datetime | None:
        """Return the moment in UTC, when the source establishes one.

        Returns:
            An aware UTC datetime when ``offset_minutes`` is set, otherwise
            ``None``. A wall reading without an offset is not an instant.
        """
        if self.offset_minutes is None:
            return None
        zone = timezone(timedelta(minutes=self.offset_minutes))
        return self.wall.replace(tzinfo=zone).astimezone(UTC)

    def render(self) -> str:
        """Render only the components the source states.

        Returns:
            ISO 8601 text: ``1985``, ``1985-07``, ``1985-07-04``,
            ``1985-07-04T10:10``, ``1985-07-04T10:10:10`` or
            ``1985-07-04T10:10:10.123000``, with ``+09:00`` appended when an
            offset is known.
        """
        wall = self.wall
        text = {
            Precision.YEAR: f"{wall:%Y}",
            Precision.MONTH: f"{wall:%Y-%m}",
            Precision.DAY: f"{wall:%Y-%m-%d}",
            Precision.MINUTE: f"{wall:%Y-%m-%dT%H:%M}",
            Precision.SECOND: f"{wall:%Y-%m-%dT%H:%M:%S}",
            Precision.SUBSECOND: f"{wall:%Y-%m-%dT%H:%M:%S}.{wall.microsecond:06d}",
        }[self.precision]
        if self.offset_minutes is None:
            return text
        sign = "-" if self.offset_minutes < 0 else "+"
        hours, minutes = divmod(abs(self.offset_minutes), 60)
        return f"{text}{sign}{hours:02d}:{minutes:02d}"


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


def _check_anchors(wall: datetime, precision: Precision) -> None:
    """Check that components below the precision hold their anchors.

    Args:
        wall: The clock reading.
        precision: How much of it the source states.

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

    Args:
        id: Stable identity from :func:`observation_id`.
        claim: What the value is about.
        source: Where it was read.
        locator: The exact field, for example ``"ExifIFD:DateTimeOriginal"``,
            ``"ftyp:major_brand"`` or ``"fs:st_birthtime"``. Repeated
            occurrences of one field get distinct locators. XML fields are
            named by namespace URI and local name, never by prefix.
        origin: The media file, or the sidecar the value came from.
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

    id: str
    claim: Claim
    source: SourceKind
    locator: str
    origin: Path
    raw: str | None
    value: TimeValue | str | None
    status: ParseStatus
    family: str
    reader: str
    reader_version: str


ReadIssueStatus = Literal["backend-unavailable", "unreadable", "failed", "file-changed"]
"""Why a read produced less than it should have."""


class ReadIssue(BaseModel):
    """A reason some evidence is missing or may be stale.

    Args:
        reader: The reader concerned, or ``"winnow"`` for the collector.
        status: What went wrong.
        message: A human-readable detail.
    """

    model_config = _MODEL_CONFIG

    reader: str
    status: ReadIssueStatus
    message: str


class FileSnapshot(BaseModel):
    """A file's ``stat`` identity at one moment.

    Equal snapshots are a freshness hint, never proof the content is
    unchanged; a consumer that needs exact identity reads the file again.

    Args:
        path: The file, as given to the collector.
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

    path: Path
    size: NonNegativeInt
    mtime_ns: int
    device: NonNegativeInt
    inode: NonNegativeInt
    ctime_ns: int
    birthtime_ns: int | None = None


class EvidenceSet(BaseModel):
    """Everything one collection observed about one file.

    ``EvidenceSet.model_dump_json()`` is the stored and ``--json`` form.

    Args:
        schema_version: :data:`EVIDENCE_SCHEMA_VERSION` when written.
        snapshot: The file's identity before reading; ``None`` only when the
            file could not be stat'ed, and then ``issues`` says so.
        observations: Every observation, in collection order.
        issues: Reasons evidence is missing or may be stale.
        dependencies: Every other file read, sidecars and ambiguous
            candidates included (#402).
        association_fingerprint: Fingerprint of the directory names and
            sidecar matching rules used (#402).
    """

    model_config = _MODEL_CONFIG

    schema_version: int = EVIDENCE_SCHEMA_VERSION
    snapshot: FileSnapshot | None
    observations: tuple[Observation, ...]
    issues: tuple[ReadIssue, ...]
    dependencies: tuple[FileSnapshot, ...] = ()
    association_fingerprint: str | None = None


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

    observation: NonNegativeInt
    reason: str


class Decision(BaseModel):
    """What a rule decided for one claim, kept apart from the evidence.

    Indices refer to the :class:`EvidenceSet` the decision was made from and
    are local to it; durable references use observation IDs.

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
    value: TimeValue | str | None
    certainty: Certainty
    rule_id: str
    rule_version: int
    chosen: tuple[NonNegativeInt, ...]
    rejected: tuple[Rejection, ...]
    conflicts: tuple[NonNegativeInt, ...]
    warnings: tuple[str, ...] = ()
    incomparable: tuple[NonNegativeInt, ...] = ()


def snapshot_fingerprint(snapshot: FileSnapshot | None) -> str | None:
    """Fingerprint a snapshot for :func:`observation_id`.

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
    schema version, the source snapshot's fingerprint, the origin, the exact
    locator, the raw value, the parsed value, the status and the reader
    fingerprint (``reader`` and ``reader_version``). The observation's
    current ``id``, its position and the rest of the collection are not part
    of it, so reordering a collection cannot change it.

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
        "origin": str(observation.origin),
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
    "TimeValue",
    "observation_id",
    "snapshot_fingerprint",
]
