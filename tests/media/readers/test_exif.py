"""Tests for the still-image EXIF, GPS and IPTC evidence reader (#395 PR A1)."""

from __future__ import annotations

import io
import struct
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import exifread
import pytest
from assertpy import assert_that
from PIL import Image, TiffImagePlugin

from winnow.media import detect
from winnow.media.evidence import collect_evidence
from winnow.media.image import heif_encoding_supported
from winnow.media.readers import ExifReader
from winnow.models.evidence import (
    Claim,
    EvidenceSet,
    Observation,
    ParseStatus,
    Precision,
    SourceKind,
    TimeValue,
)

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_READ_SOURCES = frozenset({SourceKind.EXIF, SourceKind.IPTC})
_POINTERS = (_EXIF_IFD, _GPS_IFD)


def _rational(numerator: int, denominator: int) -> TiffImagePlugin.IFDRational:
    """Build a Pillow rational.

    Args:
        numerator: Numerator.
        denominator: Denominator.

    Returns:
        The rational.
    """
    return TiffImagePlugin.IFDRational(numerator, denominator)


def _exif(
    *,
    ifd0: dict[int, object] | None = None,
    exif_ifd: dict[int, object] | None = None,
    gps: dict[int, object] | None = None,
) -> Image.Exif:
    """Build an EXIF block.

    Args:
        ifd0: IFD0 tags.
        exif_ifd: ExifIFD tags.
        gps: GPS IFD tags.

    Returns:
        The block.
    """
    exif = Image.Exif()
    for tag, value in (ifd0 or {}).items():
        exif[tag] = value
    for pointer, tags in ((_EXIF_IFD, exif_ifd), (_GPS_IFD, gps)):
        if tags:
            exif.get_ifd(pointer).update(tags)
    return exif


def _iim(dataset: int, value: bytes) -> bytes:
    """Encode one IPTC IIM record 2 dataset.

    Args:
        dataset: The dataset number, such as 55 for ``DateCreated``.
        value: Its bytes.

    Returns:
        The encoded dataset.
    """
    return bytes([0x1C, 2, dataset]) + struct.pack(">H", len(value)) + value


def _with_iptc(jpeg: bytes, iim: bytes) -> bytes:
    """Insert an APP13 Photoshop IPTC resource after a JPEG's SOI marker.

    Args:
        jpeg: The JPEG bytes.
        iim: The IIM block; the resource's size is its exact length and a
            pad byte follows an odd one, as Photoshop writes it.

    Returns:
        The JPEG with the block.
    """
    resource = (
        b"8BIM"
        + struct.pack(">H", 0x0404)
        + b"\0\0"
        + struct.pack(">I", len(iim))
        + iim
        + b"\0" * (len(iim) % 2)
    )
    segment = b"Photoshop 3.0\0" + resource
    marker = b"\xff\xed" + struct.pack(">H", len(segment) + 2) + segment
    return jpeg[:2] + marker + jpeg[2:]


def _write_jpeg(
    path: Path,
    exif: Image.Exif | bytes | None = None,
    iptc: list[tuple[int, bytes]] | None = None,
    *,
    iim: bytes | None = None,
) -> Path:
    """Write a tiny JPEG with optional EXIF and IPTC.

    Args:
        path: Where to write it.
        exif: Its EXIF block, or the raw ``APP1`` payload.
        iptc: Its IPTC datasets.
        iim: A raw IIM block, used instead of ``iptc``.

    Returns:
        The path.
    """
    buffer = io.BytesIO()
    image = Image.new("RGB", (8, 8))
    if exif is None:
        image.save(buffer, format="JPEG")
    else:
        image.save(buffer, format="JPEG", exif=exif)
    data = buffer.getvalue()
    if iptc is not None:
        iim = b"".join(_iim(dataset, value) for dataset, value in iptc)
    path.write_bytes(data if iim is None else _with_iptc(data, iim))
    return path


def _collect(path: Path) -> EvidenceSet:
    """Collect a file's evidence with the EXIF reader only.

    Args:
        path: The file.

    Returns:
        The evidence.
    """
    return collect_evidence(path, readers=[ExifReader()])


def _read(path: Path) -> list[Observation]:
    """Return the EXIF reader's observations about a file.

    Args:
        path: The file.

    Returns:
        Its EXIF and IPTC observations, in collection order.
    """
    evidence = _collect(path)
    return [o for o in evidence.observations if o.source in _READ_SOURCES]


def _at(observations: list[Observation], locator: str) -> Observation:
    """Return the one observation at a locator.

    Args:
        observations: The observations.
        locator: The locator.

    Returns:
        The observation.
    """
    found = [o for o in observations if o.locator == locator]
    assert_that(found).is_length(1)
    return found[0]


def _time(observation: Observation) -> TimeValue:
    """Return an observation's time value.

    Args:
        observation: The observation.

    Returns:
        Its value, checked to be a :class:`TimeValue`.
    """
    value = observation.value
    if not isinstance(value, TimeValue):
        pytest.fail(f"{observation.locator} holds {value!r}, not a time value")
    return value


@pytest.fixture
def set_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Switch the process timezone, restoring it afterwards.

    Yields:
        A function that sets ``TZ`` and applies it with ``time.tzset``.
    """

    def apply(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield apply
    monkeypatch.undo()
    time.tzset()


def test_exif_reader_keeps_offset_and_subsec(tmp_path: Path) -> None:
    """DateTimeOriginal with its offset and sub-seconds is one precise value."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            exif_ifd={0x9003: "2019:07:04 10:10:10", 0x9291: "123", 0x9011: "+09:00"},
        ),
    )

    observations = _read(path)

    original = _at(observations, "ExifIFD:DateTimeOriginal#0")
    value = _time(original)
    assert_that(original.claim).is_equal_to(Claim.CAPTURE_TIME)
    assert_that(original.raw).is_equal_to("2019:07:04 10:10:10 | 123 | +09:00")
    assert_that(value.wall).is_equal_to(datetime(2019, 7, 4, 10, 10, 10, 123000))
    assert_that(value.offset_minutes).is_equal_to(540)
    assert_that(value.offset_origin).is_equal_to("source")
    assert_that(value.precision).is_equal_to(Precision.SUBSECOND)
    assert_that(value.fraction_digits).is_equal_to(3)
    companions = [
        _at(observations, f"ExifIFD:{n}#0")
        for n in ("SubSecTimeOriginal", "OffsetTimeOriginal")
    ]
    assert_that([(o.claim, o.raw, o.status) for o in companions]).is_equal_to(
        [
            (Claim.UNKNOWN, "123", ParseStatus.OK),
            (Claim.UNKNOWN, "+09:00", ParseStatus.OK),
        ],
    )


def test_exif_reader_labels_ifd0_datetime_as_modified(tmp_path: Path) -> None:
    """IFD0 DateTime alone is one modified time and never a capture time."""
    path = _write_jpeg(tmp_path / "a.jpg", _exif(ifd0={0x0132: "2021:01:01 09:00:00"}))

    observations = _read(path)

    assert_that([o.claim for o in observations]).is_equal_to([Claim.MODIFIED_TIME])
    modified = _time(observations[0])
    assert_that(modified.wall).is_equal_to(datetime(2021, 1, 1, 9))
    assert_that(modified.offset_minutes).is_none()
    assert_that(modified.precision).is_equal_to(Precision.SECOND)


def test_exif_reader_applies_exif_ifd_companions_to_ifd0_datetime(
    tmp_path: Path,
) -> None:
    """IFD0 DateTime takes its offset and sub-seconds from ExifIFD."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            ifd0={0x0132: "2021:01:01 09:00:00"},
            exif_ifd={0x9290: "5", 0x9010: "-05:30"},
        ),
    )

    modified = _at(_read(path), "IFD0:DateTime#0")

    assert_that(modified.claim).is_equal_to(Claim.MODIFIED_TIME)
    assert_that(_time(modified).render()).is_equal_to("2021-01-01T09:00:00.5-05:30")


def test_exif_reader_reads_digitized(tmp_path: Path) -> None:
    """DateTimeDigitized is a digitized time, read even without DateTimeOriginal."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(exif_ifd={0x9004: "2019:07:04 10:10:10", 0x9012: "+02:00"}),
    )

    observations = _read(path)

    digitized = _at(observations, "ExifIFD:DateTimeDigitized#0")
    assert_that(digitized.claim).is_equal_to(Claim.DIGITIZED_TIME)
    assert_that(_time(digitized).render()).is_equal_to("2019-07-04T10:10:10+02:00")
    assert_that([o.claim for o in observations]).does_not_contain(Claim.CAPTURE_TIME)


def test_exif_reader_reads_gps_utc(tmp_path: Path) -> None:
    """GPS date and time are one UTC capture value with fractional seconds."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            gps={
                0x001D: "2019:07:04",
                0x0007: (_rational(1, 1), _rational(10, 1), _rational(1234, 100)),
            },
        ),
    )

    observations = _read(path)

    gps = _at(observations, "GPS:GPSDateStamp+GPSTimeStamp#0")
    value = _time(gps)
    assert_that(gps.claim).is_equal_to(Claim.CAPTURE_TIME)
    assert_that(gps.family).is_equal_to("exif-gps")
    assert_that(gps.raw).is_equal_to("2019:07:04 | 1/1 10/1 617/50")
    assert_that(value.render()).is_equal_to("2019-07-04T01:10:12.34+00:00")
    assert_that(value.offset_origin).is_equal_to("utc-by-spec")
    assert_that(
        [o.status for o in observations if o.claim is Claim.UNKNOWN],
    ).is_equal_to([ParseStatus.OK, ParseStatus.OK])


def test_exif_reader_gps_without_time_is_empty(tmp_path: Path) -> None:
    """A GPS date without a time gives an empty capture value and keeps the date."""
    path = _write_jpeg(tmp_path / "a.jpg", _exif(gps={0x001D: "2019:07:04"}))

    observations = _read(path)

    gps = _at(observations, "GPS:GPSDateStamp+GPSTimeStamp#0")
    assert_that((gps.status, gps.raw, gps.value)).is_equal_to(
        (ParseStatus.EMPTY, "2019:07:04", None),
    )
    date = _at(observations, "GPS:GPSDateStamp#0")
    assert_that((date.status, date.value)).is_equal_to((ParseStatus.OK, "2019:07:04"))


def test_exif_reader_malformed_gps_rational_keeps_raw(tmp_path: Path) -> None:
    """A zero denominator makes the GPS time malformed; the raw values stay."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            exif_ifd={0x9003: "2019:07:04 10:10:10"},
            gps={
                0x001D: "2019:07:04",
                0x0007: (_rational(1, 1), _rational(10, 0), _rational(12, 1)),
            },
        ),
    )

    observations = _read(path)

    gps = _at(observations, "GPS:GPSDateStamp+GPSTimeStamp#0")
    assert_that((gps.status, gps.raw)).is_equal_to(
        (ParseStatus.MALFORMED, "2019:07:04 | 1/1 10/0 12/1"),
    )
    time_stamp = _at(observations, "GPS:GPSTimeStamp#0")
    assert_that(time_stamp.status).is_equal_to(ParseStatus.MALFORMED)
    assert_that(_at(observations, "GPS:GPSDateStamp#0").status).is_equal_to(
        ParseStatus.OK,
    )
    original = _at(observations, "ExifIFD:DateTimeOriginal#0")
    assert_that(original.status).is_equal_to(ParseStatus.OK)


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ("0000:00:00 00:00:00", ParseStatus.PLACEHOLDER),
        ("    :  :     :  :  ", ParseStatus.PLACEHOLDER),
        ("", ParseStatus.EMPTY),
    ],
    ids=["zeros", "blanks", "empty"],
)
def test_exif_reader_placeholder_values(
    tmp_path: Path,
    raw: str,
    status: ParseStatus,
) -> None:
    """Placeholder and empty dates are kept, with their raw text, as no value."""
    path = _write_jpeg(tmp_path / "a.jpg", _exif(exif_ifd={0x9003: raw}))

    original = _at(_read(path), "ExifIFD:DateTimeOriginal#0")

    assert_that((original.raw, original.status, original.value)).is_equal_to(
        (raw, status, None),
    )


@pytest.mark.parametrize(
    ("raw", "rendered", "precision"),
    [
        ("2019:07:04   :  :  ", "2019-07-04", Precision.DAY),
        ("2019:07:     :  :  ", "2019-07", Precision.MONTH),
        ("2019:  :     :  :  ", "2019", Precision.YEAR),
    ],
    ids=["date_only", "month_only", "year_only"],
)
def test_exif_reader_partial_dates(
    tmp_path: Path,
    raw: str,
    rendered: str,
    precision: Precision,
) -> None:
    """Blank EXIF components give a partial date; nothing is filled in."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(exif_ifd={0x9003: raw, 0x9011: "+09:00"}),
    )

    original = _at(_read(path), "ExifIFD:DateTimeOriginal#0")
    value = _time(original)

    assert_that(value.render()).is_equal_to(rendered)
    assert_that(value.precision).is_equal_to(precision)
    assert_that(value.offset_minutes).is_none()
    assert_that(original.raw).is_equal_to(raw)


def test_exif_reader_malformed_date_keeps_raw(tmp_path: Path) -> None:
    """A malformed date is kept with its raw text and no value."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(exif_ifd={0x9003: "2019:13:45 10:10:10", 0x9004: "yesterday"}),
    )

    observations = _read(path)

    assert_that(
        [(o.raw, o.status, o.value) for o in observations],
    ).is_equal_to(
        [
            ("2019:13:45 10:10:10", ParseStatus.MALFORMED, None),
            ("yesterday", ParseStatus.MALFORMED, None),
        ],
    )


def test_exif_reader_invalid_companions_keep_the_base_date(tmp_path: Path) -> None:
    """An invalid offset or sub-second value is kept apart from a valid date."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            exif_ifd={
                0x9003: "2019:07:04 10:10:10",
                0x9291: "12a",
                0x9011: "+25:00",
            },
        ),
    )

    observations = _read(path)

    original = _at(observations, "ExifIFD:DateTimeOriginal#0")
    assert_that(original.raw).is_equal_to("2019:07:04 10:10:10")
    assert_that(_time(original).render()).is_equal_to("2019-07-04T10:10:10")
    for name, raw in (("SubSecTimeOriginal", "12a"), ("OffsetTimeOriginal", "+25:00")):
        companion = _at(observations, f"ExifIFD:{name}#0")
        assert_that((companion.claim, companion.raw, companion.status)).is_equal_to(
            (Claim.UNKNOWN, raw, ParseStatus.MALFORMED),
        )


def test_exif_reader_lone_offset_cannot_date_a_file(tmp_path: Path) -> None:
    """An offset without its date is recorded but is never a time claim."""
    path = _write_jpeg(tmp_path / "a.jpg", _exif(exif_ifd={0x9011: "+09:00"}))

    observations = _read(path)

    assert_that([(o.claim, o.value) for o in observations]).is_equal_to(
        [(Claim.UNKNOWN, "+09:00")],
    )


def test_exif_reader_keeps_the_same_moment_in_two_tags(tmp_path: Path) -> None:
    """Equal values in two tags are two observations; neither is dropped."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            exif_ifd={
                0x9003: "2019:07:04 10:10:10",
                0x9004: "2019:07:04 10:10:10",
                0x9011: "+09:00",
            },
            gps={
                0x001D: "2019:07:04",
                0x0007: (_rational(1, 1), _rational(10, 1), _rational(10, 1)),
            },
        ),
    )

    observations = _read(path)

    original = _time(_at(observations, "ExifIFD:DateTimeOriginal#0"))
    digitized = _time(_at(observations, "ExifIFD:DateTimeDigitized#0"))
    gps = _time(_at(observations, "GPS:GPSDateStamp+GPSTimeStamp#0"))
    assert_that(digitized.wall).is_equal_to(original.wall)
    assert_that(digitized.offset_minutes).is_none()
    assert_that(gps.instant()).is_equal_to(original.instant())
    assert_that(len({o.id for o in observations})).is_equal_to(len(observations))


def test_exif_reader_keeps_conflicting_tags(tmp_path: Path) -> None:
    """Contradicting capture tags are all kept; the reader chooses none."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(exif_ifd={0x9003: "2019:07:04 10:10:10"}),
        iptc=[(55, b"20200101"), (60, b"080000+0100")],
    )

    observations = _read(path)

    captures = [o for o in observations if o.claim is Claim.CAPTURE_TIME]
    assert_that([_time(o).render() for o in captures]).is_equal_to(
        ["2019-07-04T10:10:10", "2020-01-01T08:00:00+01:00"],
    )
    assert_that([o.source for o in captures]).is_equal_to(
        [SourceKind.EXIF, SourceKind.IPTC],
    )


def test_exif_reader_file_without_dates(fixtures_dir: Path) -> None:
    """An image with no metadata gives no observations and no issues."""
    evidence = _collect(fixtures_dir / "sample.png")

    assert_that(
        [o for o in evidence.observations if o.source in _READ_SOURCES]
    ).is_empty()
    assert_that(evidence.issues).is_empty()


def test_exif_reader_skips_files_that_are_not_images(fixtures_dir: Path) -> None:
    """Audio and video files are not read."""
    path = fixtures_dir / "sample.mp3"

    observations, issues = ExifReader().read(path, detection=detect(path))

    assert_that(observations).is_empty()
    assert_that(issues).is_empty()


def test_exif_reader_reads_text_fields(tmp_path: Path) -> None:
    """Software, make, model and the image unique id are recorded as text."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            ifd0={0x0131: "Editor 1.0", 0x010F: "Apple", 0x0110: "iPhone 8\0"},
            exif_ifd={0xA420: "   "},
        ),
    )

    observations = _read(path)

    assert_that(
        [(o.claim, o.locator, o.value, o.status) for o in observations],
    ).is_equal_to(
        [
            (Claim.SOFTWARE, "IFD0:Software#0", "Editor 1.0", ParseStatus.OK),
            (Claim.DEVICE, "IFD0:Make#0", "Apple", ParseStatus.OK),
            (Claim.DEVICE, "IFD0:Model#0", "iPhone 8", ParseStatus.OK),
            (Claim.CONTENT_ID, "ExifIFD:ImageUniqueID#0", None, ParseStatus.EMPTY),
        ],
    )


def _apple_maker_note(identifier: str) -> bytes:
    """Build an Apple MakerNote holding a content identifier in tag 0x0011.

    Args:
        identifier: The identifier.

    Returns:
        The MakerNote bytes.
    """
    payload = identifier.encode("ascii") + b"\x00"
    entry = struct.pack(">HHII", 0x0011, 2, len(payload), 14 + 2 + 12 + 4)
    ifd = struct.pack(">H", 1) + entry + struct.pack(">I", 0)
    return b"Apple iOS\x00" + b"\x00\x01" + b"MM" + ifd + payload


def test_exif_reader_reads_apple_content_identifier(tmp_path: Path) -> None:
    """The Apple MakerNote identifier keeps the parser that decoded it."""
    identifier = "A1B2C3D4-E5F6-4711-8899-AABBCCDDEEFF"
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(exif_ifd={0x927C: _apple_maker_note(identifier)}),
    )

    content_id = _at(_read(path), "MakerNotes:Apple:Tag 0x0011#0")

    assert_that(
        (content_id.claim, content_id.value, content_id.reader, content_id.family),
    ).is_equal_to((Claim.CONTENT_ID, identifier, "winnow", "apple-makernote"))


_UUID = "A1B2C3D4-E5F6-4711-8899-AABBCCDDEEFF"


def _issues(evidence: EvidenceSet) -> list[str]:
    """Return the blocks the EXIF reader reported as damaged.

    Args:
        evidence: The evidence.

    Returns:
        Each issue's block name, in order.
    """
    return [
        issue.message.split(" damaged:")[0]
        for issue in evidence.issues
        if issue.reader == "exif"
    ]


def _before_frame(data: bytes) -> bytes:
    """Cut a JPEG written by Pillow just before its quantisation tables.

    Pillow cannot open the result; its APP1 and APP13 segments are intact.

    Args:
        data: The JPEG bytes.

    Returns:
        The truncated bytes.
    """
    return data[: data.index(b"\xff\xdb")]


def _write_tiff(
    path: Path,
    exif: Image.Exif,
    iptc: bytes | None = None,
    *,
    as_longs: bool = False,
) -> Path:
    """Write a tiny TIFF with EXIF and an optional IPTC-NAA tag.

    Pillow ignores ``exif=`` when ``tiffinfo=`` is given, so the EXIF tags
    go into the TIFF directory itself, sub-directories as nested mappings.

    Args:
        path: Where to write it.
        exif: Its EXIF block.
        iptc: The tag 33723 bytes; a multiple of four with ``as_longs``.
        as_longs: Retype the tag as LONG values, as Photoshop writes it;
            Pillow's writer keeps one value of a LONG tag 33723, so the
            UNDEFINED entry it writes is patched in place.

    Returns:
        The path.
    """
    directory = TiffImagePlugin.ImageFileDirectory_v2()
    for tag, value in exif.items():
        if tag not in _POINTERS:
            directory[tag] = value
    for pointer in _POINTERS:
        if sub_directory := dict(exif.get_ifd(pointer)):
            directory[pointer] = sub_directory
    if iptc is not None:
        directory[33723] = iptc
        directory.tagtype[33723] = 7
    Image.new("RGB", (8, 8)).save(path, format="TIFF", tiffinfo=directory)
    if iptc is not None and as_longs:
        data = path.read_bytes()
        entry = struct.pack("<HHI", 33723, 7, len(iptc))
        longs = struct.pack("<HHI", 33723, 4, len(iptc) // 4)
        assert_that(data.count(entry)).is_equal_to(1)
        path.write_bytes(data.replace(entry, longs))
    return path


def _refuse_pixels(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Lower Pillow's pixel limit until it refuses to open ``path``.

    Args:
        monkeypatch: Restores the limit afterwards.
        path: An 8x8 image.
    """
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(Image.DecompressionBombError):
        Image.open(path)


def _summary(observations: list[Observation]) -> list[tuple[object, ...]]:
    """Reduce observations to what two backends must agree on.

    Args:
        observations: The observations.

    Returns:
        Claim, locator, raw value, value and status of each.
    """
    return [(o.claim, o.locator, o.raw, o.value, o.status) for o in observations]


def test_exif_reader_apple_identifier_is_the_same_on_every_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every backend decodes the Apple note's original bytes the same way."""
    exif = _exif(ifd0={0x010F: "Apple"}, exif_ifd={0x927C: _apple_maker_note(_UUID)})
    jpeg = _write_jpeg(tmp_path / "whole.jpg", exif)
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(_before_frame(jpeg.read_bytes()))
    with pytest.raises(OSError, match="truncated|cannot identify"):
        Image.open(cut).load()
    tiff = _write_tiff(tmp_path / "a.tiff", exif)
    by_pillow = _read(tiff)
    _refuse_pixels(monkeypatch, tiff)

    found = [_read(path) for path in (jpeg, cut, tiff)] + [by_pillow]

    identifiers = [
        [
            (o.locator, o.value, o.status)
            for o in observations
            if o.claim is Claim.CONTENT_ID
        ]
        for observations in found
    ]
    expected = [("MakerNotes:Apple:Tag 0x0011#0", _UUID, ParseStatus.OK)]
    assert_that(identifiers).is_equal_to([expected] * 4)


def test_exif_reader_keeps_iptc_when_pillow_refuses_the_pixels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JPEG above Pillow's pixel limit keeps its EXIF and IPTC dates."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(exif_ifd={0x9003: "2019:07:04 10:10:10"}),
        iptc=[(55, b"20180704"), (60, b"101010+0900")],
    )
    _refuse_pixels(monkeypatch, path)

    observations = _read(path)

    created = _time(_at(observations, "IPTC:DateCreated#0"))
    assert_that(created.render()).is_equal_to("2018-07-04T10:10:10+09:00")
    assert_that(_at(observations, "ExifIFD:DateTimeOriginal#0").status).is_equal_to(
        ParseStatus.OK,
    )


def test_exif_reader_keeps_iptc_of_a_truncated_jpeg(tmp_path: Path) -> None:
    """A JPEG cut before its image data keeps its IPTC dates and no issue."""
    whole = _write_jpeg(
        tmp_path / "whole.jpg",
        _exif(exif_ifd={0x9003: "2019:07:04 10:10:10"}),
        iptc=[(55, b"20180704"), (60, b"101010+0900")],
    )
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(_before_frame(whole.read_bytes()))

    evidence = _collect(cut)

    observations = [o for o in evidence.observations if o.source in _READ_SOURCES]
    assert_that(_summary(observations)).is_equal_to(_summary(_read(whole)))
    assert_that(_issues(evidence)).is_empty()


def test_exif_reader_reads_tiff_with_exifread_like_pillow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exifread reads the same fields, at the same locators, as Pillow does."""
    exif = _exif(
        ifd0={0x0132: "2021:01:01 09:00:00", 0x010F: "Apple"},
        exif_ifd={0x9003: "2019:07:04 10:10:10", 0x9291: "123", 0x9011: "+09:00"},
        gps={
            0x001D: "2019:07:04",
            0x0007: (_rational(1, 1), _rational(10, 1), _rational(1234, 100)),
        },
    )
    path = _write_tiff(tmp_path / "a.tiff", exif, iptc=_iim(55, b"20180704"))
    by_pillow = _read(path)
    _refuse_pixels(monkeypatch, path)

    by_exifread = _read(path)

    assert_that(_summary(by_exifread)).is_equal_to(_summary(by_pillow))
    assert_that([o.locator for o in by_pillow]).contains(
        "ExifIFD:DateTimeOriginal#0",
        "GPS:GPSDateStamp+GPSTimeStamp#0",
        "IFD0:DateTime#0",
    )
    assert_that(
        {o.reader for o in by_exifread if o.source is SourceKind.EXIF}
    ).is_equal_to(
        {"exifread"},
    )
    assert_that(_at(by_pillow, "IPTC:DateCreated#0").status).is_equal_to(ParseStatus.OK)


@pytest.mark.parametrize("pixels_refused", [False, True], ids=["pillow", "exifread"])
def test_exif_reader_reads_tiff_iptc_stored_as_longs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pixels_refused: bool,
) -> None:
    """IPTC stored as TIFF LONG values is turned back into its bytes."""
    block = _iim(55, b"20180704") + _iim(60, b"101010+0900")
    block += b"\0" * (-len(block) % 4)
    path = _write_tiff(tmp_path / "a.tiff", _exif(), iptc=block, as_longs=True)
    if pixels_refused:
        _refuse_pixels(monkeypatch, path)

    created = _at(_read(path), "IPTC:DateCreated#0")

    assert_that(_time(created).render()).is_equal_to("2018-07-04T10:10:10+09:00")


def test_exif_reader_reports_exifread_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither backend can read the file, the reader reports a failure."""
    path = _write_tiff(tmp_path / "a.tiff", _exif())
    _refuse_pixels(monkeypatch, path)

    def broken(*args: object, **kwargs: object) -> object:
        raise ValueError("broken")

    monkeypatch.setattr(exifread, "process_file", broken)

    assert_that(_issues(_collect(path))).is_equal_to(["exifread"])


def _cut_exif_ifd(exif: Image.Exif) -> bytes:
    """Cut an EXIF block in the middle of its ExifIFD's second entry.

    Args:
        exif: An EXIF block whose ExifIFD holds at least two entries.

    Returns:
        The damaged bytes, ``Exif`` header included.
    """
    data: bytes = exif.tobytes()
    parsed = Image.Exif()
    parsed.load(data)
    offset = parsed[_EXIF_IFD]
    if not isinstance(offset, int):
        pytest.fail(f"ExifIFD pointer is {offset!r}")
    return data[: 6 + offset + 2 + 12 + 6]


def test_exif_reader_reports_a_corrupt_exif_directory(tmp_path: Path) -> None:
    """An ExifIFD cut short is reported; IFD0 and IPTC are still read."""
    damaged = _cut_exif_ifd(
        _exif(
            ifd0={0x0132: "2021:01:01 09:00:00"},
            exif_ifd={0x9003: "2019:07:04 10:10:10", 0x9004: "2019:07:04 10:10:10"},
        ),
    )
    path = _write_jpeg(tmp_path / "a.jpg", damaged, iptc=[(55, b"20180704")])

    evidence = _collect(path)

    observations = [o for o in evidence.observations if o.source in _READ_SOURCES]
    assert_that([o.locator for o in observations]).contains(
        "IFD0:DateTime#0",
        "IPTC:DateCreated#0",
    )
    assert_that(_issues(evidence)).contains("ExifIFD")


def test_exif_reader_reports_a_malformed_exif_header(tmp_path: Path) -> None:
    """An EXIF block whose TIFF header is wrong is reported, not read as absent."""
    data = _exif(ifd0={0x0132: "2021:01:01 09:00:00"}).tobytes()
    damaged = data[:6] + b"XX" + data[8:]
    path = _write_jpeg(tmp_path / "a.jpg", damaged, iptc=[(55, b"20180704")])

    evidence = _collect(path)

    observations = [o for o in evidence.observations if o.source in _READ_SOURCES]
    assert_that([o.locator for o in observations]).is_equal_to(["IPTC:DateCreated#0"])
    assert_that(_issues(evidence)).is_equal_to(["EXIF"])


@pytest.mark.parametrize(
    "note",
    [
        _apple_maker_note(_UUID)[:12] + b"XX" + _apple_maker_note(_UUID)[14:],
        _apple_maker_note(_UUID)[:-10],
    ],
    ids=["bad_byte_order", "value_cut_short"],
)
def test_exif_reader_reports_a_damaged_apple_note(tmp_path: Path, note: bytes) -> None:
    """A damaged Apple note gives an issue and no identifier; other fields stay."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(exif_ifd={0x9003: "2019:07:04 10:10:10", 0x927C: note}),
    )

    evidence = _collect(path)

    observations = [o for o in evidence.observations if o.source in _READ_SOURCES]
    assert_that([o.claim for o in observations]).is_equal_to([Claim.CAPTURE_TIME])
    assert_that(_issues(evidence)).is_equal_to(["MakerNote"])


def test_exif_reader_ignores_another_vendors_maker_note(tmp_path: Path) -> None:
    """Tag 0x0011 of a non-Apple note is not read as an Apple identifier."""
    note = b"Canon\x00\x00\x01MM" + struct.pack(">H", 0)
    path = _write_jpeg(tmp_path / "a.jpg", _exif(exif_ifd={0x927C: note}))

    evidence = _collect(path)

    assert_that(
        [o for o in evidence.observations if o.source in _READ_SOURCES]
    ).is_empty()
    assert_that(evidence.issues).is_empty()


@pytest.mark.parametrize(
    "tail",
    [
        bytes([0x1C, 2, 60, 0]),
        bytes([0x1C, 2, 60]) + struct.pack(">H", 12) + b"101010+0900",
    ],
    ids=["header_cut_short", "length_overstated"],
)
def test_exif_reader_keeps_iptc_date_before_a_damaged_time(
    tmp_path: Path,
    tail: bytes,
) -> None:
    """A complete DateCreated survives a damaged TimeCreated at day precision."""
    path = _write_jpeg(tmp_path / "a.jpg", iim=_iim(55, b"20180704") + tail)

    evidence = _collect(path)

    observations = [o for o in evidence.observations if o.source in _READ_SOURCES]
    assert_that(_summary(observations)).is_length(1)
    created = _at(observations, "IPTC:DateCreated#0")
    assert_that((created.raw, _time(created).render())).is_equal_to(
        ("20180704", "2018-07-04"),
    )
    assert_that(_issues(evidence)).is_equal_to(["IPTC"])


def test_exif_reader_keeps_an_empty_iptc_dataset(tmp_path: Path) -> None:
    """A zero-length DateCreated is kept as an empty observation."""
    path = _write_jpeg(tmp_path / "a.jpg", iptc=[(55, b"")])

    created = _at(_read(path), "IPTC:DateCreated#0")

    assert_that((created.raw, created.status)).is_equal_to(("", ParseStatus.EMPTY))


def test_exif_reader_reports_a_cut_short_exif_segment(tmp_path: Path) -> None:
    """A file ending inside its EXIF segment is reported as damaged."""
    whole = _write_jpeg(tmp_path / "whole.jpg", _exif(ifd0={0x010F: "Apple"}))
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(whole.read_bytes()[:30])

    assert_that(_issues(_collect(cut))).is_equal_to(["JPEG segments"])


def test_exif_reader_iptc_date_only_keeps_day_precision(tmp_path: Path) -> None:
    """IPTC DateCreated without TimeCreated stays a day."""
    path = _write_jpeg(tmp_path / "a.jpg", iptc=[(55, b"20190704")])

    created = _at(_read(path), "IPTC:DateCreated#0")

    assert_that(created.claim).is_equal_to(Claim.CAPTURE_TIME)
    assert_that(_time(created).precision).is_equal_to(Precision.DAY)
    assert_that(created.raw).is_equal_to("20190704")


def test_exif_reader_iptc_repeated_datasets_are_not_paired(tmp_path: Path) -> None:
    """Repeated DateCreated values are each kept and none takes the time."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        iptc=[(55, b"20190704"), (55, b"20190000"), (60, b"10101")],
    )

    observations = _read(path)

    assert_that(
        [(o.locator, o.raw, o.status) for o in observations],
    ).is_equal_to(
        [
            ("IPTC:DateCreated#0", "20190704", ParseStatus.OK),
            ("IPTC:DateCreated#1", "20190000", ParseStatus.OK),
            ("IPTC:TimeCreated#0", "10101", ParseStatus.MALFORMED),
        ],
    )
    assert_that(_time(observations[1]).render()).is_equal_to("2019")


def test_exif_reader_iptc_non_ascii_bytes_are_hex(tmp_path: Path) -> None:
    """IPTC bytes that are not ASCII are stored as hex and are malformed."""
    path = _write_jpeg(tmp_path / "a.jpg", iptc=[(55, b"\xff\xfe"), (60, b"\xe9")])

    observations = _read(path)

    assert_that([(o.raw, o.status) for o in observations]).is_equal_to(
        [("hex:fffe", ParseStatus.MALFORMED), ("hex:e9", ParseStatus.MALFORMED)],
    )


@pytest.mark.skipif(
    not heif_encoding_supported(),
    reason="pillow-heif HEIF encoder unavailable",
)
def test_exif_reader_reads_heic(tmp_path: Path) -> None:
    """A HEIC still's EXIF dates are read through pillow-heif."""
    path = tmp_path / "a.heic"
    exif = _exif(exif_ifd={0x9003: "2019:07:04 10:10:10", 0x9011: "+09:00"})
    Image.new("RGB", (8, 8)).save(path, format="HEIF", exif=exif.tobytes())

    original = _at(_read(path), "ExifIFD:DateTimeOriginal#0")

    assert_that(_time(original).render()).is_equal_to("2019-07-04T10:10:10+09:00")


@pytest.mark.parametrize("image_format", ["PNG", "WEBP", "TIFF"])
def test_exif_reader_reads_other_formats(tmp_path: Path, image_format: str) -> None:
    """PNG, WebP and TIFF EXIF dates are read the same way as JPEG's."""
    path = tmp_path / f"a.{image_format.lower()}"
    exif = _exif(exif_ifd={0x9003: "2019:07:04 10:10:10"})
    Image.new("RGB", (8, 8)).save(path, format=image_format, exif=exif.tobytes())

    original = _at(_read(path), "ExifIFD:DateTimeOriginal#0")

    assert_that(_time(original).render()).is_equal_to("2019-07-04T10:10:10")


def test_observations_independent_of_tz(
    tmp_path: Path,
    set_tz: Callable[[str], None],
) -> None:
    """Observations are byte-identical under two different host timezones."""
    path = _write_jpeg(
        tmp_path / "a.jpg",
        _exif(
            ifd0={0x0132: "2021:01:01 09:00:00"},
            exif_ifd={0x9003: "2019:07:04 10:10:10", 0x9291: "123", 0x9011: "+09:00"},
            gps={
                0x001D: "2019:07:04",
                0x0007: (_rational(1, 1), _rational(10, 1), _rational(12, 1)),
            },
        ),
        iptc=[(55, b"20190704"), (60, b"101010+0900")],
    )

    dumps = []
    for zone in ("UTC", "Pacific/Kiritimati"):
        set_tz(zone)
        dumps.append(_collect(path).model_dump_json())

    assert_that(dumps[1]).is_equal_to(dumps[0])


def test_exif_reader_version_names_every_backend() -> None:
    """The reader version names winnow and both backends."""
    assert_that(ExifReader().version()).contains("winnow ", "pillow ", "exifread ")
