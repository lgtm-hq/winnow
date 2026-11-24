"""Date and identity evidence from a still image's EXIF and IPTC (#395 PR A1).

:class:`ExifReader` records every date-bearing EXIF, GPS and IPTC field of
an image, and the software, device and content-identifier fields the
resolver (#396) uses with them. It decides nothing.

Backends:

- JPEG: a bounded walk of the marker segments
  (:func:`~winnow.media.readers._segments.scan_jpeg`) takes the EXIF
  ``APP1`` and the IPTC resources of ``APP13``; Pillow's EXIF parser reads
  the former and winnow's IIM parser the latter. The pixels are never
  decoded, so a truncated JPEG or one above Pillow's pixel limit keeps its
  metadata.
- Every other image Pillow opens (TIFF, DNG and TIFF-based RAW Pillow
  reads, HEIC/HEIF through ``pillow-heif``, AVIF, WebP, PNG): Pillow's EXIF
  parser, and for TIFF the IPTC-NAA tag 33723.
- An image Pillow cannot open: exifread, including its raw MakerNote and
  IPTC-NAA bytes.

Damage is reported, never read as absence: each block (JPEG segments, EXIF,
ExifIFD, GPS, MakerNote, IPTC) is read on its own, and an exception or a
Pillow corruption warning while reading it gives one ``failed`` issue for
that block while every value already read is kept. Limit: EXIF holds each
tag once per directory, and the backends keep one value per tag, so a
directory that repeats a tag yields its last copy only.

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
4. The Apple MakerNote content identifier (``content_id``) at
   ``MakerNotes:Apple:Tag 0x0011#0``, decoded by winnow's Apple parser
   (:func:`~winnow.media.image.decode_apple_maker_note`) from the note's
   original bytes on every path; a damaged Apple note gives an issue and no
   identifier.
5. IPTC: each 2:55 ``DateCreated`` (``capture_time``), with the 2:60
   ``TimeCreated`` applied when the file holds exactly one complete copy of
   each and it parses, then each ``TimeCreated`` as its own ``unknown``
   observation. Datasets before a damaged one are kept.

Raw values are stored as the backend gives them: text as it decoded it,
bytes as ``hex:``, rationals as ``n/d`` separated by spaces. Pillow and
exifread decode ASCII tags themselves (dropping the terminating NUL).
"""

from __future__ import annotations

import numbers
import struct
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, TypeVar

import exifread
import PIL
from loguru import logger
from PIL import Image, ImageFile

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
from winnow.media.image import decode_apple_maker_note
from winnow.media.readers._segments import JpegMetadata, iim_datasets, scan_jpeg
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
_JPEG: Final[str] = "jpeg"
_EXIF_IFD: Final[int] = 0x8769
_GPS_IFD: Final[int] = 0x8825
_MAKER_NOTE: Final[tuple[str, int]] = ("ExifIFD", 0x927C)
_TIFF: Final[str] = "TIFF"
_IPTC_DATE_CREATED: Final[tuple[int, int]] = (2, 55)
_IPTC_TIME_CREATED: Final[tuple[int, int]] = (2, 60)
_APPLE_CONTENT_ID: Final[str] = "Tag 0x0011"
_APPLE_LOCATOR: Final[str] = f"MakerNotes:Apple:{_APPLE_CONTENT_ID}#0"
_EXIFREAD_MAKER_NOTE: Final[str] = "EXIF MakerNote"
_EXIFREAD_IPTC: Final[str] = "Image IPTC/NAA"
_LITTLE_ENDIAN: Final[bytes] = b"II"
_BYTE_TYPES: Final[frozenset[int]] = frozenset({1, 7})
_LONG_TYPE: Final[int] = 4
_BYTE_MAX: Final[int] = 0xFF
_LONG_MAX: Final[int] = 0xFFFFFFFF

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
    See the module docstring for the backends, the observations and their
    order.

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
            The observations in documented order and the blocks that are
            damaged or could not be read.
        """
        if detection.media_type is not MediaType.IMAGE:
            return [], []
        issues: list[ReadIssue] = []
        if detection.container == _JPEG:
            return _read_jpeg(path, issues), issues
        try:
            image = Image.open(path)
        except Exception as exc:
            logger.debug("Pillow cannot open {} ({}); using exifread", path, exc)
            return _read_with_exifread(path, issues), issues
        with image:
            return _read_with_pillow(path, image, issues), issues


def _read_jpeg(path: Path, issues: list[ReadIssue]) -> list[Observation]:
    """Read a JPEG's EXIF and IPTC segments without decoding the image.

    Args:
        path: The file.
        issues: Receives a ``failed`` issue per damaged block.

    Returns:
        The observations.
    """
    metadata = _guarded("JPEG segments", issues, partial(_scan, path))
    if metadata is None:
        return []
    if metadata.problems:
        issues.append(_failed("JPEG segments", "; ".join(metadata.problems)))
    observations: list[Observation] = []
    exif = metadata.exif
    if exif is not None:
        observations.extend(_exif_block(path, issues, partial(_load_exif, exif)))
    observations.extend(_iptc_observations(path, metadata.iptc, issues))
    return observations


def _scan(path: Path) -> JpegMetadata:
    """Walk a JPEG's marker segments.

    Args:
        path: The file.

    Returns:
        What the segments hold.
    """
    with path.open("rb") as handle:
        return scan_jpeg(handle)


def _read_with_pillow(
    path: Path,
    image: ImageFile.ImageFile,
    issues: list[ReadIssue],
) -> list[Observation]:
    """Read a non-JPEG image's EXIF, and TIFF IPTC, through Pillow.

    Args:
        path: The file.
        image: The opened image.
        issues: Receives a ``failed`` issue per damaged block.

    Returns:
        The observations.
    """
    observations = _exif_block(path, issues, partial(_image_exif, image))
    if image.format == _TIFF:
        observations.extend(_iptc_observations(path, _tiff_iptc(path, issues), issues))
    return observations


def _read_with_exifread(path: Path, issues: list[ReadIssue]) -> list[Observation]:
    """Read EXIF, the MakerNote and IPTC with exifread.

    Args:
        path: The file, which Pillow cannot open.
        issues: Receives a ``failed`` issue when exifread fails or a block's
            bytes cannot be recovered.

    Returns:
        The observations.
    """
    processed = _guarded("exifread", issues, partial(_exifread_tags, path))
    if processed is None:
        return []
    writer = _Writer(path, SourceKind.EXIF, "exif", "exifread", exifread.__version__)
    values: dict[tuple[str, int], object] = {
        (tag.ifd, tag.tag): getattr(processed[tag.exifread], "values", None)
        for tag in _ALL_TAGS
        if tag.exifread in processed
    }
    little = _byte_order(path) == _LITTLE_ENDIAN
    observations = _exif_observations(_Tags(values, writer))
    note = processed.get(_EXIFREAD_MAKER_NOTE)
    if note is not None:
        observations.extend(
            _maker_note_observations(
                path, _exifread_bytes(note, little=little), issues
            ),
        )
    iptc = _exifread_iptc(processed, issues, little=little)
    observations.extend(_iptc_observations(path, iptc, issues))
    return observations


def _exifread_iptc(
    processed: Mapping[str, object],
    issues: list[ReadIssue],
    *,
    little: bool,
) -> tuple[bytes, ...]:
    """Take the IPTC-NAA block (TIFF tag 33723) from exifread's tags.

    Args:
        processed: exifread's tags.
        issues: Receives a ``failed`` issue when the bytes cannot be
            recovered.
        little: Whether the file is little-endian.

    Returns:
        The block, or nothing.
    """
    tag = processed.get(_EXIFREAD_IPTC)
    if tag is None:
        return ()
    data = _exifread_bytes(tag, little=little)
    if data is None:
        issues.append(_failed("IPTC", "tag 33723 holds no IPTC-NAA bytes"))
        return ()
    return (data,)


def _exifread_tags(path: Path) -> dict[str, object]:
    """Run exifread without thumbnails, keeping every value whole.

    ``details`` stays on: without it exifread drops the raw MakerNote.

    Args:
        path: The file.

    Returns:
        exifread's tags.
    """
    with path.open("rb") as handle:
        return dict(
            exifread.process_file(
                handle,
                details=True,
                extract_thumbnail=False,
                truncate_tags=False,
            ),
        )


def _byte_order(path: Path) -> bytes:
    """Read a TIFF-structured file's byte-order mark.

    Args:
        path: The file.

    Returns:
        Its first two bytes, ``II`` for little-endian.
    """
    with path.open("rb") as handle:
        return handle.read(2)


def _exifread_bytes(tag: object, *, little: bool) -> bytes | None:
    """Recover the stored bytes of an exifread tag.

    Args:
        tag: An exifread tag of byte, undefined or long type.
        little: Whether the file is little-endian, for long values.

    Returns:
        The bytes, or ``None`` when they cannot be recovered.
    """
    values = getattr(tag, "values", None)
    field_type = getattr(tag, "field_type", None)
    return _stored_bytes(
        values,
        int(field_type) if isinstance(field_type, int) else None,
        little=little,
    )


def _stored_bytes(
    values: object, field_type: int | None, *, little: bool
) -> bytes | None:
    """Turn a decoded TIFF value back into the bytes the file stores.

    Args:
        values: Bytes, or the integers of a byte, undefined or long field.
        field_type: The TIFF field type.
        little: Whether long values are little-endian.

    Returns:
        The bytes, or ``None`` for any other type or an out-of-range value.
    """
    if isinstance(values, bytes):
        return values
    if not isinstance(values, list | tuple) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in values
    ):
        return None
    integers: list[int] = list(values)
    if field_type in _BYTE_TYPES and all(0 <= v <= _BYTE_MAX for v in integers):
        return bytes(integers)
    if field_type == _LONG_TYPE and all(0 <= v <= _LONG_MAX for v in integers):
        return struct.pack(f"{'<' if little else '>'}{len(integers)}I", *integers)
    return None


def _failed(block: str, detail: str) -> ReadIssue:
    """Describe a damaged or unreadable block.

    Args:
        block: The block's name.
        detail: What went wrong.

    Returns:
        A ``failed`` issue for this reader.
    """
    return ReadIssue(
        reader=READER_NAME,
        status="failed",
        message=f"{block} damaged: {detail}",
    )


@contextmanager
def _block(name: str, issues: list[ReadIssue]) -> Iterator[None]:
    """Read one block, reporting an exception or a corruption warning.

    Pillow reports damage it reads past (``Corrupt EXIF data``, ``Truncated
    File Read``) as a warning and keeps the rest; those warnings, and any
    exception, become one ``failed`` issue for the block. Values the caller
    stored before the problem are kept.

    Args:
        name: The block's name.
        issues: Receives the issue.

    Yields:
        Nothing; the block runs inside.
    """
    problems: list[str] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            yield
        except Exception as exc:
            problems.append(f"{type(exc).__name__}: {exc}")
    damage = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    if damage or problems:
        issues.append(_failed(name, "; ".join([*damage, *problems])))


def _guarded(
    block: str,
    issues: list[ReadIssue],
    read: Callable[[], _T],
) -> _T | None:
    """Read one block inside :func:`_block`.

    Args:
        block: The block's name, for the issue.
        issues: Receives the issue.
        read: Reads the block.

    Returns:
        What ``read`` returned, or ``None`` when it raised.
    """
    with _block(block, issues):
        return read()
    return None


def _load_exif(data: bytes) -> Image.Exif:
    """Parse EXIF bytes with Pillow, as it does for an opened image.

    Args:
        data: The payload, with or without its ``Exif\\0\\0`` header.

    Returns:
        The parsed block.
    """
    exif = Image.Exif()
    exif.load(data)
    return exif


def _image_exif(image: ImageFile.ImageFile) -> Image.Exif:
    """Parse an opened image's EXIF.

    Args:
        image: The opened image.

    Returns:
        The block, parsed from the image's raw EXIF bytes when it exposes
        them, so damage raises here instead of being hidden.
    """
    data = image.info.get("exif")
    if isinstance(data, bytes):
        return _load_exif(data)
    return image.getexif()


def _exif_block(
    path: Path,
    issues: list[ReadIssue],
    load: Callable[[], Image.Exif],
) -> list[Observation]:
    """Read IFD0, ExifIFD, GPS and the MakerNote, each block on its own.

    Args:
        path: The file.
        issues: Receives a ``failed`` issue per damaged block.
        load: Parses the EXIF block.

    Returns:
        The EXIF observations, then the Apple content identifier.
    """
    writer = _Writer(path, SourceKind.EXIF, "exif", "pillow", PIL.__version__)
    values: dict[tuple[str, int], object] = {}
    exif = _guarded("EXIF", issues, load)
    if exif is not None:
        values.update(_entries("IFD0", exif))
        for name, pointer in (("ExifIFD", _EXIF_IFD), ("GPS", _GPS_IFD)):
            directory = _guarded(name, issues, partial(_directory, exif, pointer))
            values.update(_entries(name, directory))
    observations = _exif_observations(_Tags(values, writer))
    note = values.get(_MAKER_NOTE)
    if note is not None:
        data = note if isinstance(note, bytes) else None
        observations.extend(_maker_note_observations(path, data, issues))
    return observations


def _entries(
    ifd: str,
    directory: Mapping[int, object] | None,
) -> Iterator[tuple[tuple[str, int], object]]:
    """Key a directory's values by directory name and tag.

    Args:
        ifd: The directory's name.
        directory: Its tags, or ``None`` when it could not be read.

    Yields:
        ``((ifd, tag), value)`` pairs.
    """
    for tag, value in (directory or {}).items():
        yield (ifd, tag), value


def _directory(exif: Image.Exif, pointer: int) -> dict[int, object]:
    """Read one EXIF sub-directory.

    Args:
        exif: The EXIF block.
        pointer: The tag pointing to the directory.

    Returns:
        Its tags.
    """
    directory: dict[int, object] = dict(exif.get_ifd(pointer))
    return directory


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


def _maker_note_observations(
    path: Path,
    note: bytes | None,
    issues: list[ReadIssue],
) -> list[Observation]:
    """Record the Apple content identifier from a MakerNote's original bytes.

    Args:
        path: The file.
        note: The MakerNote's bytes, or ``None`` when the backend did not
            return them whole.
        issues: Receives a ``failed`` issue for a damaged Apple note or
            bytes that cannot be recovered.

    Returns:
        One ``content_id`` observation, or none when the note is not
        Apple's, holds no identifier or is damaged.
    """
    if note is None:
        issues.append(_failed("MakerNote", "its bytes could not be recovered"))
        return []
    tags = _guarded("MakerNote", issues, partial(_apple_tags, note))
    value = None if tags is None else tags.get(_APPLE_CONTENT_ID)
    if value is None:
        return []
    writer = _Writer(path, SourceKind.EXIF, "apple-makernote", "winnow", __version__)
    return [
        writer.observe(
            Claim.CONTENT_ID,
            _APPLE_LOCATOR,
            raw=value,
            parsed=_text(value),
        ),
    ]


def _apple_tags(note: bytes) -> dict[str, str]:
    """Decode an Apple MakerNote; another vendor's note decodes to nothing.

    Args:
        note: The MakerNote's bytes.

    Returns:
        The Apple tags, empty for another vendor's note.
    """
    return decode_apple_maker_note(note) or {}


def _tiff_iptc(path: Path, issues: list[ReadIssue]) -> tuple[bytes, ...]:
    """Take a TIFF's IPTC-NAA block through exifread.

    Pillow keeps a single value of tag 33723 when Photoshop stores it as
    several LONGs, so the bytes come from exifread, which keeps them all.

    Args:
        path: The TIFF.
        issues: Receives a ``failed`` issue when exifread fails or the bytes
            cannot be recovered.

    Returns:
        The block, or nothing.
    """
    processed = _guarded("exifread", issues, partial(_exifread_tags, path))
    if processed is None:
        return ()
    little = _byte_order(path) == _LITTLE_ENDIAN
    return _exifread_iptc(processed, issues, little=little)


def _iptc_observations(
    path: Path,
    blocks: Sequence[bytes],
    issues: list[ReadIssue],
) -> list[Observation]:
    """Turn IPTC 2:55 and 2:60 into observations.

    ``TimeCreated`` is combined with ``DateCreated`` only when the file holds
    exactly one complete copy of each; repeated datasets cannot be paired
    reliably. A damaged dataset ends its block with a ``failed`` issue; the
    datasets before it are kept.

    Args:
        path: The file.
        blocks: The IIM blocks, in file order.
        issues: Receives a ``failed`` issue per damaged block.

    Returns:
        Each ``DateCreated`` then each ``TimeCreated``.
    """
    datasets = []
    for block in blocks:
        found, damage = iim_datasets(block)
        datasets.extend(found)
        if damage is not None:
            issues.append(_failed("IPTC", damage))
    dates = [d.value for d in datasets if (d.record, d.dataset) == _IPTC_DATE_CREATED]
    times = [d.value for d in datasets if (d.record, d.dataset) == _IPTC_TIME_CREATED]
    writer = _Writer(path, SourceKind.IPTC, "iptc", "winnow", __version__)
    paired = len(dates) == 1 and len(times) == 1
    observations = [
        _iptc_date(writer, index, date, times[0] if paired else None)
        for index, date in enumerate(dates)
    ]
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


def _iptc_date(
    writer: _Writer,
    index: int,
    date: bytes,
    time: bytes | None,
) -> Observation:
    """Record one ``DateCreated``, with its paired ``TimeCreated`` applied.

    Args:
        writer: Builds the observation.
        index: The dataset's occurrence number.
        date: Its bytes.
        time: The paired ``TimeCreated`` bytes, if any.

    Returns:
        The observation; its ``raw`` joins the time only when it was applied.
    """
    date_text, time_text = _ascii(date), None if time is None else _ascii(time)
    parsed: TimeValue | ParseStatus = ParseStatus.MALFORMED
    if date_text is not None:
        parsed = parse_iptc_value(date_text, time_text)
    timed = isinstance(parsed, TimeValue) and parsed.precision is Precision.SECOND
    parts = [date, time] if timed and time is not None else [date]
    return writer.observe(
        Claim.CAPTURE_TIME,
        f"IPTC:DateCreated#{index}",
        raw=_JOIN.join(_iptc_raw(part) for part in parts),
        parsed=parsed,
    )


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
