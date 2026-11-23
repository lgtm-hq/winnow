"""Build evidence sets for the date resolver tests (#396).

Every input is an :class:`EvidenceSet` built with the public model
constructors; no sample file is read. Locators follow the shapes #395 and
#402 describe; the resolver matches the field names they contain.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from winnow.media.resolve_date import DatePolicy
from winnow.models.evidence import (
    Claim,
    Decision,
    EvidenceSet,
    FileSnapshot,
    Observation,
    OffsetOrigin,
    ParseStatus,
    Precision,
    ReadIssue,
    SourceKind,
    TimeValue,
    observation_id,
)

FILE: Final[Path] = Path("/photos/2019/IMG_0001.JPG")
TAKEOUT_JSON: Final[Path] = Path("/photos/2019/IMG_0001.JPG.supplemental-metadata.json")
XMP_SIDECAR: Final[Path] = Path("/photos/2019/IMG_0001.xmp")
SNAPSHOT: Final[FileSnapshot] = FileSnapshot(
    path=FILE,
    size=2048,
    mtime_ns=1_562_231_410_000_000_000,
    device=1,
    inode=7,
    ctime_ns=1_562_231_410_000_000_000,
)
NOW: Final[datetime] = datetime(2026, 11, 14, tzinfo=UTC)

_SIDECARS: Final[frozenset[SourceKind]] = frozenset(
    {SourceKind.SIDECAR_XMP, SourceKind.SIDECAR_TAKEOUT, SourceKind.SIDECAR_AAE},
)
_TEXT: Final[re.Pattern[str]] = re.compile(
    r"(?P<y>\d{4})(?:-(?P<mo>\d{2})(?:-(?P<d>\d{2})"
    r"(?:T(?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:\.(?P<f>\d+))?)?)?)?)?",
)


def tv(
    text: str, offset: int | None = None, *, origin: OffsetOrigin = "source"
) -> TimeValue:
    """Build a time value from partial ISO text.

    Args:
        text: ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, ``...THH:MM``,
            ``...THH:MM:SS`` or ``...THH:MM:SS.f+``; the precision follows
            from how much is written.
        offset: UTC offset in minutes, or ``None`` for a wall clock.
        origin: Where the offset came from.

    Returns:
        The time value.

    Raises:
        ValueError: If the text is not one of those forms.
    """
    match = _TEXT.fullmatch(text)
    if match is None:
        raise ValueError(f"unsupported test time text: {text!r}")
    parts = match.groupdict()
    fraction = parts["f"]
    precision = _precision(parts)
    wall = datetime(
        int(parts["y"]),
        int(parts["mo"] or 1),
        int(parts["d"] or 1),
        int(parts["h"] or 0),
        int(parts["mi"] or 0),
        int(parts["s"] or 0),
        int((fraction or "0")[:6].ljust(6, "0")),
    )
    return TimeValue(
        wall=wall,
        offset_minutes=offset,
        offset_origin=None if offset is None else origin,
        precision=precision,
        fraction_digits=None if fraction is None else len(fraction),
    )


def _precision(parts: dict[str, str | None]) -> Precision:
    """Name the precision of parsed text parts.

    Args:
        parts: The regular expression groups.

    Returns:
        The finest stated component.
    """
    order = (
        ("f", Precision.SUBSECOND),
        ("s", Precision.SECOND),
        ("mi", Precision.MINUTE),
        ("d", Precision.DAY),
        ("mo", Precision.MONTH),
    )
    for group, precision in order:
        if parts[group] is not None:
            return precision
    return Precision.YEAR


def obs(
    claim: Claim,
    source: SourceKind,
    locator: str,
    wall: str | None,
    offset: int | None = None,
    family: str = "exif",
    **fields: Any,
) -> Observation:
    """Build an observation with its stable identity.

    Args:
        claim: What it is about.
        source: Where it was read.
        locator: The exact field.
        wall: Partial ISO text of the value (see :func:`tv`), or ``None``
            for no parsed value.
        offset: UTC offset in minutes, or ``None``.
        family: Provenance family.
        **fields: ``offset_origin``, ``origin``, ``raw``, ``status``,
            ``value`` (overrides ``wall``) or ``reader``.

    Returns:
        The observation, with ``id`` from :func:`observation_id`.
    """
    origin_kind: OffsetOrigin = fields.pop("offset_origin", "source")
    value: TimeValue | str | None = fields.pop(
        "value",
        None if wall is None else tv(wall, offset, origin=origin_kind),
    )
    default_origin = FILE
    if source is SourceKind.SIDECAR_TAKEOUT:
        default_origin = TAKEOUT_JSON
    elif source is SourceKind.SIDECAR_XMP:
        default_origin = XMP_SIDECAR
    observation = Observation(
        id="",
        claim=claim,
        source=source,
        locator=locator,
        origin=fields.pop("origin", default_origin),
        raw=fields.pop("raw", wall),
        value=value,
        status=fields.pop("status", ParseStatus.OK),
        family=family,
        reader=fields.pop("reader", "test"),
        reader_version="1",
    )
    if fields:
        raise TypeError(f"unknown fields: {sorted(fields)}")
    return observation.model_copy(
        update={"id": observation_id(observation, snapshot=SNAPSHOT)},
    )


def dto(
    wall: str,
    offset: int | None = None,
    *,
    source: SourceKind = SourceKind.EXIF,
    locator: str | None = None,
    **fields: Any,
) -> Observation:
    """Build an embedded ``DateTimeOriginal`` observation.

    Args:
        wall: The value text.
        offset: Its offset from the file, or ``None``.
        source: ``exif`` or ``xmp``.
        locator: The locator; defaults to the source's field.
        **fields: Passed to :func:`obs`.

    Returns:
        The observation.
    """
    if locator is None:
        locator = (
            "ExifIFD:DateTimeOriginal#0"
            if source is SourceKind.EXIF
            else "xmp:http://ns.adobe.com/exif/1.0/:DateTimeOriginal#0"
        )
    return obs(
        Claim.CAPTURE_TIME,
        source,
        locator,
        wall,
        offset,
        family=str(source),
        **fields,
    )


def gps(wall: str) -> Observation:
    """Build an EXIF GPS date-and-time observation, UTC by specification.

    Args:
        wall: The UTC value text.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.EXIF,
        "GPS:GPSDateStamp+GPSTimeStamp#0",
        wall,
        0,
        offset_origin="utc-by-spec",
    )


def apple(wall: str, offset: int | None = 540, *, occurrence: int = 0) -> Observation:
    """Build a QuickTime ``com.apple.quicktime.creationdate`` observation.

    Args:
        wall: The value text.
        offset: Its explicit offset.
        occurrence: The occurrence counter.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.QUICKTIME,
        f"QuickTime:Keys:com.apple.quicktime.creationdate#{occurrence}",
        wall,
        offset,
        family="quicktime-keys",
    )


def container(wall: str, **fields: Any) -> Observation:
    """Build a demuxed container creation time, UTC by specification.

    Args:
        wall: The value text, or an empty string for no parsed value.
        **fields: Passed to :func:`obs`.

    Returns:
        The observation.
    """
    return obs(
        Claim.CONTAINER_CREATED_TIME,
        SourceKind.QUICKTIME,
        "ffprobe:format.tags.creation_time#0",
        wall or None,
        0 if wall else None,
        family="quicktime-container",
        offset_origin="utc-by-spec",
        **fields,
    )


def iptc(wall: str, offset: int | None = None) -> Observation:
    """Build an IPTC 2:55/2:60 date-created observation.

    Args:
        wall: The value text.
        offset: Its offset, or ``None``.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.IPTC,
        "IPTC:2:55+2:60#0",
        wall,
        offset,
        family="iptc",
    )


def xmp_created(wall: str, offset: int | None = None) -> Observation:
    """Build an embedded XMP ``photoshop:DateCreated`` observation.

    Args:
        wall: The value text.
        offset: Its offset, or ``None``.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.XMP,
        "xmp:http://ns.adobe.com/photoshop/1.0/:DateCreated#0",
        wall,
        offset,
        family="xmp",
    )


def takeout(wall: str, *, origin: Path = TAKEOUT_JSON) -> Observation:
    """Build a Takeout ``photoTakenTime`` observation, UTC by specification.

    Args:
        wall: The UTC value text.
        origin: The JSON sidecar it came from.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.SIDECAR_TAKEOUT,
        "takeout:photoTakenTime.timestamp",
        wall,
        0,
        family="takeout",
        offset_origin="utc-by-spec",
        origin=origin,
    )


def sidecar_xmp(wall: str, offset: int | None = None) -> Observation:
    """Build an XMP sidecar ``exif:DateTimeOriginal`` observation.

    Args:
        wall: The value text.
        offset: Its offset, or ``None``.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.SIDECAR_XMP,
        "xmp:http://ns.adobe.com/exif/1.0/:DateTimeOriginal#0",
        wall,
        offset,
        family="xmp-sidecar",
    )


def png(wall: str) -> Observation:
    """Build a PNG text ``Creation Time`` observation.

    Args:
        wall: The value text.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.PNG_TEXT,
        "png:tEXt:Creation Time#0",
        wall,
        family="png-text",
    )


def filename(wall: str) -> Observation:
    """Build a camera file-name pattern observation.

    Args:
        wall: The value text.

    Returns:
        The observation.
    """
    return obs(
        Claim.CAPTURE_TIME,
        SourceKind.FILENAME,
        "filename:IMG_YYYYMMDD_HHMMSS",
        wall,
        family="filename",
        raw="IMG_20190704_101010.jpg",
    )


def hint(claim: Claim, wall: str = "2021-01-01T09:00:00", **fields: Any) -> Observation:
    """Build a date observation of another claim, as its reader would.

    Args:
        claim: The claim.
        wall: The value text.
        **fields: Passed to :func:`obs`.

    Returns:
        The observation.
    """
    sources = {
        Claim.FILE_CREATED_TIME: (SourceKind.FILESYSTEM, "fs:st_birthtime"),
        Claim.FILE_MODIFIED_TIME: (SourceKind.FILESYSTEM, "fs:st_mtime_ns"),
        Claim.FILE_CHANGED_TIME: (SourceKind.FILESYSTEM, "fs:st_ctime_ns"),
        Claim.RECEIVED_TIME: (SourceKind.FILENAME, "filename:IMG-YYYYMMDD-WA####"),
        Claim.MODIFIED_TIME: (SourceKind.EXIF, "IFD0:DateTime#0"),
        Claim.DIGITIZED_TIME: (SourceKind.EXIF, "ExifIFD:DateTimeDigitized#0"),
    }
    source, locator = sources.get(claim, (SourceKind.XMP, f"xmp:{claim}#0"))
    return obs(claim, source, locator, wall, family=str(source), **fields)


def device(text: str, locator: str) -> Observation:
    """Build a camera make or model observation.

    Args:
        text: The make or model.
        locator: The field.

    Returns:
        The observation.
    """
    return obs(Claim.DEVICE, SourceKind.EXIF, locator, None, value=text, raw=text)


def software(text: str) -> Observation:
    """Build an IFD0 ``Software`` observation.

    Args:
        text: The software name.

    Returns:
        The observation.
    """
    return obs(
        Claim.SOFTWARE,
        SourceKind.EXIF,
        "IFD0:Software#0",
        None,
        value=text,
        raw=text,
    )


def evidence(
    *observations: Observation,
    associate: bool = True,
    issues: tuple[ReadIssue, ...] = (),
) -> EvidenceSet:
    """Build an evidence set for :data:`FILE`.

    Args:
        *observations: The observations, in collection order.
        associate: Record every sidecar origin as an associated dependency.
        issues: Read issues.

    Returns:
        The evidence set.
    """
    sidecars = sorted(
        {o.origin for o in observations if o.source in _SIDECARS},
        key=str,
    )
    dependencies = tuple(
        SNAPSHOT.model_copy(update={"path": path, "inode": 100 + number})
        for number, path in enumerate(sidecars)
    )
    return EvidenceSet(
        snapshot=SNAPSHOT,
        observations=observations,
        issues=issues,
        dependencies=dependencies if associate else (),
        association_fingerprint="sha256:test" if associate and sidecars else None,
    )


def policy(**overrides: Any) -> DatePolicy:
    """Build the test policy with the fixed clock.

    Args:
        **overrides: Fields to replace.

    Returns:
        The policy.
    """
    fields: dict[str, Any] = {"now": NOW}
    fields.update(overrides)
    return DatePolicy(**fields)


def semantic(decision: Decision, evidence_set: EvidenceSet) -> dict[str, Any]:
    """Describe a decision by observation identity instead of index.

    Args:
        decision: The decision.
        evidence_set: The evidence it was made from.

    Returns:
        A comparable description that survives reordering observations.
    """
    ids = [o.id for o in evidence_set.observations]
    return {
        "claim": decision.claim,
        "value": decision.value,
        "certainty": decision.certainty,
        "rule_id": decision.rule_id,
        "rule_version": decision.rule_version,
        "warnings": decision.warnings,
        "chosen": sorted(ids[i] for i in decision.chosen),
        "conflicts": sorted(ids[i] for i in decision.conflicts),
        "incomparable": sorted(ids[i] for i in decision.incomparable),
        "rejected": sorted((ids[r.observation], r.reason) for r in decision.rejected),
    }


def time_value(decision: Decision) -> TimeValue:
    """Return a decision's time value.

    Args:
        decision: A decision with a time value.

    Returns:
        The value.

    Raises:
        AssertionError: If the decision has no time value.
    """
    if not isinstance(decision.value, TimeValue):
        raise AssertionError(f"decision has no time value: {decision.value!r}")
    return decision.value


def reasons(decision: Decision) -> dict[int, str]:
    """Map each rejected index to its reason.

    Args:
        decision: The decision.

    Returns:
        Index to reason code.
    """
    return {r.observation: r.reason for r in decision.rejected}
