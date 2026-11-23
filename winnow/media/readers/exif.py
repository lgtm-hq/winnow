"""Date and identity evidence from a still image's EXIF and IPTC (#395 PR A1).

:class:`ExifReader` records every date-bearing EXIF, GPS and IPTC field of
an image, and the software, device and content-identifier fields the
resolver (#396) uses with them. It decides nothing.

Backends: Pillow for every image it opens (JPEG, TIFF, DNG and TIFF-based
RAW Pillow reads, HEIC/HEIF through ``pillow-heif``, AVIF, WebP, PNG);
exifread for an image Pillow cannot open. IPTC is read through Pillow only.
Each block (IFD0, ExifIFD, GPS, IPTC, MakerNote) is read on its own: a block
that cannot be read gives a ``failed`` issue and the others are still read.

Observations, in this order, each only when its field is present:

1. For ``DateTimeOriginal`` (``capture_time``), ``DateTimeDigitized``
   (``digitized_time``) and IFD0 ``DateTime`` (``modified_time``): one
   observation of the date with its ``SubSecTime*`` and ``OffsetTime*``
   companions applied when they are valid, at the date's own locator; its
   ``raw`` is the date's text followed by `` | `` and each companion's text
   that was applied. Then each companion present, as its own ``unknown``
   observation with its own status, so a lone offset or sub-second value can
   never date a file.
2. GPS: one ``capture_time`` observation at
   ``GPS:GPSDateStamp+GPSTimeStamp#0`` in UTC by specification (``empty``
   when either part is absent), then each part present as its own
   ``unknown`` observation.
3. IFD0 ``Software`` (``software``), ``Make`` and ``Model`` (``device``),
   ExifIFD ``ImageUniqueID`` (``content_id``).
4. The Apple MakerNote content identifier (``content_id``), from winnow's
   own parser of the note Pillow returns, or from exifread when Pillow
   found no Apple note, as :func:`~winnow.media.image.read_maker_note_tags`
   does; the locator names which.
5. IPTC: each 2:55 ``DateCreated`` (``capture_time``), with the 2:60
   ``TimeCreated`` applied when the file holds exactly one of each and it
   parses, then each ``TimeCreated`` as its own ``unknown`` observation.

Raw values are stored as read: text as the backend decoded it, bytes as
``hex:``, rationals as ``n/d`` separated by spaces.
"""

from __future__ import annotations

import numbers
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, TypeVar

import exifread
import PIL
from loguru import logger
from PIL import Image, ImageFile, IptcImagePlugin

from winnow import __version__
from winnow.media._dates import (
    parse_exif_date_value,
    parse_exif_datetime_value,
    parse_exif_offset,
    parse_exif_subsec,
    parse_gps_datetime_value,
    parse_gps_time,
    parse_iptc_time,
    parse_iptc_value,
)
from winnow.media._detect_base import DetectionResult
from winnow.media.image import apple_maker_note_tags
from winnow.models.evidence import (
    HEX_PREFIX,
    Claim,
    Observation,
    ParseStatus,
    Precision,
    ReadIssue,
    SourceKind,
    TimeValue,
)
from winnow.models.media import MediaType

READER_NAME: Final[str] = "exif"
"""Name of :class:`ExifReader`, used on its issues."""

_JOIN: Final[str] = " | "
_PADDING: Final[str] = " \x00"
_EXIF_IFD: Final[int] = 0x8769
_GPS_IFD: Final[int] = 0x8825
_IPTC_DATE_CREATED: Final[tuple[int, int]] = (2, 55)
_IPTC_TIME_CREATED: Final[tuple[int, int]] = (2, 60)
_APPLE_CONTENT_ID: Final[str] = "Tag 0x0011"
_EXIFREAD_MAKER_NOTE: Final[str] = f"MakerNote {_APPLE_CONTENT_ID}"

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _Tag:
    """One EXIF tag.

    Args:
        ifd: The directory it lives in: ``IFD0``, ``ExifIFD`` or ``GPS``.
        tag: Its numeric id.
        name: Its EXIF name.
        exifread: exifread's key for it.
    """

    ifd: str
    tag: int
    name: str
    exifread: str

    @property
    def locator(self) -> str:
        """Return the tag's locator.

        Returns:
            ``<ifd>:<name>#0``; EXIF holds a tag once per directory.
        """
        return f"{self.ifd}:{self.name}#0"


@dataclass(frozen=True, slots=True)
class _DateField:
    """An EXIF date with its sub-second and offset companions.

    Args:
        claim: What the date is evidence of.
        base: The date tag.
        subsec: Its ``SubSecTime*`` tag.
        offset: Its ``OffsetTime*`` tag.
    """

    claim: Claim
    base: _Tag
    subsec: _Tag
    offset: _Tag


def _exif_tag(tag: int, name: str) -> _Tag:
    """Describe an ExifIFD tag.

    Args:
        tag: Its numeric id.
        name: Its EXIF name.

    Returns:
        The tag.
    """
    return _Tag("ExifIFD", tag, name, f"EXIF {name}")


_DATE_FIELDS: Final[tuple[_DateField, ...]] = (
    _DateField(
        Claim.CAPTURE_TIME,
        _exif_tag(0x9003, "DateTimeOriginal"),
        _exif_tag(0x9291, "SubSecTimeOriginal"),
        _exif_tag(0x9011, "OffsetTimeOriginal"),
    ),
    _DateField(
        Claim.DIGITIZED_TIME,
        _exif_tag(0x9004, "DateTimeDigitized"),
        _exif_tag(0x9292, "SubSecTimeDigitized"),
        _exif_tag(0x9012, "OffsetTimeDigitized"),
    ),
    _DateField(
        Claim.MODIFIED_TIME,
        _Tag("IFD0", 0x0132, "DateTime", "Image DateTime"),
        _exif_tag(0x9290, "SubSecTime"),
        _exif_tag(0x9010, "OffsetTime"),
    ),
)
"""EXIF dates; the offset and sub-second tags live in ExifIFD even for the
IFD0 ``DateTime`` they qualify."""

_GPS_DATE: Final[_Tag] = _Tag("GPS", 0x001D, "GPSDateStamp", "GPS GPSDate")
_GPS_TIME: Final[_Tag] = _Tag("GPS", 0x0007, "GPSTimeStamp", "GPS GPSTimeStamp")
_GPS_LOCATOR: Final[str] = "GPS:GPSDateStamp+GPSTimeStamp#0"

_TEXT_FIELDS: Final[tuple[tuple[Claim, _Tag], ...]] = (
    (Claim.SOFTWARE, _Tag("IFD0", 0x0131, "Software", "Image Software")),
    (Claim.DEVICE, _Tag("IFD0", 0x010F, "Make", "Image Make")),
    (Claim.DEVICE, _Tag("IFD0", 0x0110, "Model", "Image Model")),
    (Claim.CONTENT_ID, _exif_tag(0xA420, "ImageUniqueID")),
)

_ALL_TAGS: Final[tuple[_Tag, ...]] = (
    *(tag for date in _DATE_FIELDS for tag in (date.base, date.subsec, date.offset)),
    _GPS_DATE,
    _GPS_TIME,
    *(tag for _, tag in _TEXT_FIELDS),
)


@dataclass(frozen=True, slots=True)
class _Writer:
    """Builds observations from one backend about one file.

    Args:
        path: The file.
        source: Where the values were read.
        family: Their provenance family.
        reader: The backend that read them.
        version: That backend's version.
    """

    path: Path
    source: SourceKind
    family: str
    reader: str
    version: str

    def observe(
        self,
        claim: Claim,
        locator: str,
        *,
        raw: str | None,
        parsed: TimeValue | str | ParseStatus,
    ) -> Observation:
        """Build one observation.

        Args:
            claim: What it is evidence of.
            locator: The exact field.
            raw: The value as read.
            parsed: The parsed value, or why there is none.

        Returns:
            The observation; its ``id`` is assigned by the collector.
        """
        status = parsed if isinstance(parsed, ParseStatus) else ParseStatus.OK
        return Observation(
            id="",
            claim=claim,
            source=self.source,
            locator=locator,
            origin=self.path,
            raw=raw,
            value=None if isinstance(parsed, ParseStatus) else parsed,
            status=status,
            family=self.family,
            reader=self.reader,
            reader_version=self.version,
        )

    def with_family(self, family: str) -> _Writer:
        """Return this writer for another provenance family.

        Args:
            family: The family.

        Returns:
            The writer.
        """
        return _Writer(self.path, self.source, family, self.reader, self.version)


@dataclass(frozen=True, slots=True)
class _Tags:
    """EXIF values one backend read, keyed by directory and tag id.

    Args:
        values: ``(ifd, tag) -> value`` for every tag present.
        writer: Builds observations attributed to that backend.
    """

    values: Mapping[tuple[str, int], object]
    writer: _Writer

    def get(self, tag: _Tag) -> object | None:
        """Return a tag's value.

        Args:
            tag: The tag.

        Returns:
            Its value, or ``None`` when absent.
        """
        return self.values.get((tag.ifd, tag.tag))


@dataclass
class ExifReader:
    """Read EXIF, GPS, IPTC and Apple MakerNote evidence from images.

    Only files detected as images are read; any other file gives nothing.
    See the module docstring for the observations and their order.

    Args:
        name: The reader's name, recorded on its issues.
    """

    name: str = READER_NAME

    def version(self) -> str:
        """Return the versions of winnow and both backends.

        Returns:
            ``winnow <v>; pillow <v>; exifread <v>``.
        """
        return (
            f"winnow {__version__}; pillow {PIL.__version__};"
            f" exifread {exifread.__version__}"
        )

    def read(
        self,
        path: Path,
        *,
        detection: DetectionResult,
    ) -> tuple[list[Observation], list[ReadIssue]]:
        """Read one image.

        Args:
            path: The file.
            detection: Its content-detection result.

        Returns:
            The observations in documented order and the blocks that could
            not be read.
        """
        if detection.media_type is not MediaType.IMAGE:
            return [], []
        issues: list[ReadIssue] = []
        try:
            image = Image.open(path)
        except Exception as exc:
            logger.debug("Pillow cannot open {} ({}); using exifread", path, exc)
            return _read_with_exifread(path, issues), issues
        with image:
            return _read_with_pillow(path, image, issues), issues


def _read_with_pillow(
    path: Path,
    image: ImageFile.ImageFile,
    issues: list[ReadIssue],
) -> list[Observation]:
    """Read every block through an opened Pillow image.

    Args:
        path: The file.
        image: The opened image.
        issues: Receives a ``failed`` issue per unreadable block.

    Returns:
        The observations.
    """
    writer = _Writer(path, SourceKind.EXIF, "exif", "pillow", PIL.__version__)
    tags = _pillow_tags(image, writer, issues)
    observations = _exif_observations(tags)
    notes = _guarded("MakerNote", issues, lambda: apple_maker_note_tags(image))
    if notes is not None:
        maker = _Writer(path, SourceKind.EXIF, "apple-makernote", "winnow", __version__)
        observations.extend(_content_ids(maker, "MakerNotes:Apple", notes))
    else:
        observations.extend(_exifread_maker_note(path, issues))
    observations.extend(_iptc_observations(path, image, issues))
    return observations


def _read_with_exifread(path: Path, issues: list[ReadIssue]) -> list[Observation]:
    """Read EXIF and the MakerNote with exifread, for images Pillow cannot open.

    Args:
        path: The file.
        issues: Receives a ``failed`` issue when exifread fails.

    Returns:
        The observations.
    """
    processed = _exifread_tags(path, issues)
    if processed is None:
        return []
    writer = _Writer(path, SourceKind.EXIF, "exif", "exifread", exifread.__version__)
    values = {
        (tag.ifd, tag.tag): getattr(processed[tag.exifread], "values", None)
        for tag in _ALL_TAGS
        if tag.exifread in processed
    }
    return [
        *_exif_observations(_Tags(values, writer)),
        *_maker_note_from_exifread(path, processed),
    ]


def _guarded(
    block: str,
    issues: list[ReadIssue],
    read: Callable[[], _T],
) -> _T | None:
    """Read one block, turning any error into a ``failed`` issue.

    Args:
        block: The block's name, for the issue.
        issues: Receives the issue.
        read: Reads the block.

    Returns:
        What ``read`` returned, or ``None`` when it raised.
    """
    try:
        return read()
    except Exception as exc:
        issues.append(
            ReadIssue(
                reader=READER_NAME,
                status="failed",
                message=f"{block} unreadable: {type(exc).__name__}: {exc}",
            ),
        )
        return None


def _pillow_tags(
    image: ImageFile.ImageFile,
    writer: _Writer,
    issues: list[ReadIssue],
) -> _Tags:
    """Collect IFD0, ExifIFD and GPS values from Pillow, block by block.

    Args:
        image: The opened image.
        writer: Attributes the values to Pillow.
        issues: Receives a ``failed`` issue per unreadable block.

    Returns:
        The values that could be read.
    """
    values: dict[tuple[str, int], object] = {}
    exif = _guarded("EXIF", issues, image.getexif)
    if exif is None:
        return _Tags(values, writer)
    values.update((("IFD0", tag), value) for tag, value in exif.items())
    for name, pointer in (("ExifIFD", _EXIF_IFD), ("GPS", _GPS_IFD)):
        directory = _guarded(name, issues, partial(_directory, exif, pointer))
        values.update(((name, tag), value) for tag, value in (directory or {}).items())
    return _Tags(values, writer)


def _directory(exif: Image.Exif, pointer: int) -> dict[int, object]:
    """Read one EXIF sub-directory.

    Args:
        exif: The EXIF block.
        pointer: The tag pointing to the directory.

    Returns:
        Its tags.
    """
    return dict(exif.get_ifd(pointer))


def _exif_observations(tags: _Tags) -> list[Observation]:
    """Turn EXIF values into observations, in documented order.

    Args:
        tags: The values one backend read.

    Returns:
        The date, GPS and text observations.
    """
    observations: list[Observation] = []
    for date in _DATE_FIELDS:
        observations.extend(_date_observations(tags, date))
    observations.extend(_gps_observations(tags))
    for claim, tag in _TEXT_FIELDS:
        value = tags.get(tag)
        if value is not None:
            observations.append(
                tags.writer.observe(
                    claim,
                    tag.locator,
                    raw=_raw(value),
                    parsed=_text(value),
                ),
            )
    return observations


def _date_observations(tags: _Tags, date: _DateField) -> Iterator[Observation]:
    """Yield an EXIF date's combined observation and its companions.

    Args:
        tags: The values one backend read.
        date: The date field.

    Yields:
        The combined observation when the date is present, then each
        companion present.
    """
    base, subsec, offset = (tags.get(t) for t in (date.base, date.subsec, date.offset))
    if base is not None:
        parsed: TimeValue | ParseStatus = ParseStatus.MALFORMED
        if isinstance(base, str):
            parsed = parse_exif_datetime_value(
                base,
                subsec=subsec if isinstance(subsec, str) else None,
                offset=offset if isinstance(offset, str) else None,
            )
        applied = _applied(parsed, subsec=subsec, offset=offset)
        yield tags.writer.observe(
            date.claim,
            date.base.locator,
            raw=_JOIN.join(_raw(part) for part in (base, *applied)),
            parsed=parsed,
        )
    for tag, value, check in (
        (date.subsec, subsec, parse_exif_subsec),
        (date.offset, offset, parse_exif_offset),
    ):
        if value is not None:
            yield _companion(tags.writer, tag.locator, value, check)


def _applied(
    parsed: TimeValue | ParseStatus,
    *,
    subsec: object | None,
    offset: object | None,
) -> list[object]:
    """Name the companions a parsed date used.

    Args:
        parsed: The parsed date.
        subsec: The sub-second value read.
        offset: The offset value read.

    Returns:
        The companion values the date includes, sub-seconds first.
    """
    if not isinstance(parsed, TimeValue):
        return []
    applied: list[object] = []
    if parsed.precision is Precision.SUBSECOND:
        applied.append(subsec)
    if parsed.offset_minutes is not None:
        applied.append(offset)
    return applied


def _companion(
    writer: _Writer,
    locator: str,
    value: object,
    check: Callable[[str], object],
) -> Observation:
    """Record a companion field on its own, as ``unknown``.

    Args:
        writer: Builds the observation.
        locator: The companion's locator.
        value: Its value as read.
        check: Its parser, returning a :class:`ParseStatus` when it fails.

    Returns:
        The observation: the padded text as value when it parses.
    """
    parsed: str | ParseStatus = ParseStatus.MALFORMED
    if isinstance(value, str):
        status = check(value)
        parsed = status if isinstance(status, ParseStatus) else value.strip(_PADDING)
    return writer.observe(Claim.UNKNOWN, locator, raw=_raw(value), parsed=parsed)


def _gps_observations(tags: _Tags) -> list[Observation]:
    """Turn ``GPSDateStamp`` and ``GPSTimeStamp`` into observations.

    Args:
        tags: The values one backend read.

    Returns:
        The combined UTC observation and each part present; nothing when
        both are absent.
    """
    date, time = tags.get(_GPS_DATE), tags.get(_GPS_TIME)
    if date is None and time is None:
        return []
    writer = tags.writer.with_family("exif-gps")
    rationals = _rationals(time)
    parsed: TimeValue | ParseStatus = ParseStatus.MALFORMED
    if (date is None or isinstance(date, str)) and (time is None or rationals):
        parsed = parse_gps_datetime_value(
            date if isinstance(date, str) else None,
            rationals,
        )
    present = [part for part in (date, time) if part is not None]
    observations = [
        writer.observe(
            Claim.CAPTURE_TIME,
            _GPS_LOCATOR,
            raw=_JOIN.join(_raw(part) for part in present),
            parsed=parsed,
        ),
    ]
    if date is not None:
        observations.append(
            _companion(writer, _GPS_DATE.locator, date, parse_exif_date_value),
        )
    if time is not None:
        time_status: str | ParseStatus = ParseStatus.MALFORMED
        if rationals is not None:
            checked = parse_gps_time(rationals)
            time_status = checked if isinstance(checked, ParseStatus) else _raw(time)
        observations.append(
            writer.observe(
                Claim.UNKNOWN,
                _GPS_TIME.locator,
                raw=_raw(time),
                parsed=time_status,
            ),
        )
    return observations


def _content_ids(
    writer: _Writer,
    group: str,
    notes: Mapping[str, str],
) -> list[Observation]:
    """Record the Apple content identifier from decoded MakerNote tags.

    Args:
        writer: Attributes it to the parser that decoded the note.
        group: The locator's group, naming that parser's view of the note.
        notes: Decoded MakerNote tags.

    Returns:
        One ``content_id`` observation, or none without the tag.
    """
    value = notes.get(_APPLE_CONTENT_ID)
    if value is None:
        return []
    return [
        writer.observe(
            Claim.CONTENT_ID,
            f"{group}:{_APPLE_CONTENT_ID}#0",
            raw=value,
            parsed=_text(value),
        ),
    ]


def _exifread_maker_note(path: Path, issues: list[ReadIssue]) -> list[Observation]:
    """Read the content identifier with exifread when Pillow found no Apple note.

    Args:
        path: The file.
        issues: Receives a ``failed`` issue when exifread fails.

    Returns:
        The observation, or none.
    """
    processed = _exifread_tags(path, issues)
    return [] if processed is None else _maker_note_from_exifread(path, processed)


def _maker_note_from_exifread(
    path: Path,
    processed: Mapping[str, object],
) -> list[Observation]:
    """Record exifread's decoded ``MakerNote Tag 0x0011``.

    Args:
        path: The file.
        processed: exifread's tags, read with details.

    Returns:
        The observation, or none.
    """
    tag = processed.get(_EXIFREAD_MAKER_NOTE)
    if tag is None:
        return []
    writer = _Writer(
        path,
        SourceKind.EXIF,
        "makernote",
        "exifread",
        exifread.__version__,
    )
    notes = {_APPLE_CONTENT_ID: str(tag)}
    return _content_ids(writer, "MakerNote", notes)


def _exifread_tags(path: Path, issues: list[ReadIssue]) -> dict[str, object] | None:
    """Run exifread with MakerNote details.

    Args:
        path: The file.
        issues: Receives a ``failed`` issue when exifread fails.

    Returns:
        exifread's tags, or ``None`` when it failed.
    """

    def read() -> dict[str, object]:
        with path.open("rb") as handle:
            return dict(exifread.process_file(handle, details=True))

    return _guarded("exifread", issues, read)


def _iptc_observations(
    path: Path,
    image: ImageFile.ImageFile,
    issues: list[ReadIssue],
) -> list[Observation]:
    """Turn IPTC 2:55 and 2:60 into observations.

    ``TimeCreated`` is combined with ``DateCreated`` only when the file holds
    exactly one of each; repeated datasets cannot be paired reliably.

    Args:
        path: The file.
        image: The opened image.
        issues: Receives a ``failed`` issue when IPTC cannot be read.

    Returns:
        Each ``DateCreated`` then each ``TimeCreated``.
    """
    info = _guarded("IPTC", issues, lambda: IptcImagePlugin.getiptcinfo(image))
    if not info:
        return []
    writer = _Writer(path, SourceKind.IPTC, "iptc", "pillow", PIL.__version__)
    dates = _datasets(info.get(_IPTC_DATE_CREATED))
    times = _datasets(info.get(_IPTC_TIME_CREATED))
    paired = len(dates) == 1 and len(times) == 1
    observations: list[Observation] = []
    for index, date in enumerate(dates):
        time = times[0] if paired else None
        date_text, time_text = _ascii(date), None if time is None else _ascii(time)
        parsed: TimeValue | ParseStatus = ParseStatus.MALFORMED
        if date_text is not None:
            parsed = parse_iptc_value(date_text, time_text)
        timed = isinstance(parsed, TimeValue) and parsed.precision is Precision.SECOND
        parts = [date, time] if timed and time is not None else [date]
        observations.append(
            writer.observe(
                Claim.CAPTURE_TIME,
                f"IPTC:DateCreated#{index}",
                raw=_JOIN.join(_iptc_raw(part) for part in parts),
                parsed=parsed,
            ),
        )
    for index, time in enumerate(times):
        text = _ascii(time)
        observations.append(
            _companion(
                writer,
                f"IPTC:TimeCreated#{index}",
                time if text is None else text,
                parse_iptc_time,
            ),
        )
    return observations


def _datasets(value: object) -> list[bytes]:
    """List an IPTC dataset's occurrences.

    Args:
        value: What ``getiptcinfo`` holds for it: bytes, a list of bytes,
            or ``None``.

    Returns:
        Its occurrences in file order.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, bytes)]
    return [value] if isinstance(value, bytes) else []


def _ascii(value: bytes) -> str | None:
    """Decode IPTC bytes that are ASCII.

    Args:
        value: The bytes.

    Returns:
        The text, or ``None`` when they are not ASCII.
    """
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None


def _iptc_raw(value: bytes) -> str:
    """Render IPTC bytes as read.

    Args:
        value: The bytes.

    Returns:
        The text when they are ASCII, otherwise ``hex:`` and the bytes.
    """
    text = _ascii(value)
    return _raw(value) if text is None else text


def _rationals(value: object) -> list[tuple[int, int]] | None:
    """Read a sequence of rationals as integer pairs.

    Args:
        value: A tuple or list of rationals.

    Returns:
        ``(numerator, denominator)`` pairs, or ``None`` when ``value`` is
        not a sequence of integer rationals.
    """
    if not isinstance(value, tuple | list):
        return None
    pairs = [_rational(item) for item in value]
    if any(pair is None for pair in pairs):
        return None
    return [pair for pair in pairs if pair is not None]


def _rational(value: object) -> tuple[int, int] | None:
    """Read one rational as an integer pair, zero denominators included.

    Args:
        value: A Pillow ``IFDRational``, an exifread ``Ratio`` or an int.

    Returns:
        The pair, or ``None`` when ``value`` is not an integer rational.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Rational):
        return None
    numerator, denominator = value.numerator, value.denominator
    if not (isinstance(numerator, int) and isinstance(denominator, int)):
        return None
    return numerator, denominator


def _raw(value: object) -> str:
    """Render a value exactly as read.

    Args:
        value: The value.

    Returns:
        Text unchanged, bytes as ``hex:``, rationals as ``n/d``, sequences
        as their items separated by spaces, anything else as ``str``.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return f"{HEX_PREFIX}{value.hex()}"
    if isinstance(value, tuple | list):
        return " ".join(_raw(item) for item in value)
    pair = None if isinstance(value, int) else _rational(value)
    return str(value) if pair is None else f"{pair[0]}/{pair[1]}"


def _text(value: object) -> str | ParseStatus:
    """Parse a text field such as ``Make`` or ``ImageUniqueID``.

    Args:
        value: The value as read.

    Returns:
        The text without NUL and space padding; ``EMPTY`` when nothing is
        left; ``MALFORMED`` when the value is not text.
    """
    if not isinstance(value, str):
        return ParseStatus.MALFORMED
    text = value.strip(_PADDING)
    return text if text else ParseStatus.EMPTY


__all__ = ["READER_NAME", "ExifReader"]
