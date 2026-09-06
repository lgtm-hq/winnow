"""Tests for the image metadata, EXIF, and thumbnail helpers."""

from __future__ import annotations

import struct
from datetime import datetime
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import ExifTags, Image

from winnow.exceptions import MediaError
from winnow.media import image as image_module
from winnow.media.image import (
    extract_image_metadata,
    generate_thumbnail,
    heif_supported,
    read_exif,
    read_maker_note_tags,
)

_STANDARD_IMAGES: list[tuple[str, str]] = [
    ("sample.jpg", "JPEG"),
    ("sample.png", "PNG"),
    ("sample.tiff", "TIFF"),
    ("sample.webp", "WEBP"),
    ("sample.gif", "GIF"),
    ("sample.bmp", "BMP"),
]


@pytest.mark.parametrize(
    ("filename", "expected_format"),
    _STANDARD_IMAGES,
    ids=[name for name, _ in _STANDARD_IMAGES],
)
def test_extract_image_metadata_reads_dimensions(
    fixtures_dir: Path,
    filename: str,
    expected_format: str,
) -> None:
    """Standard raster formats yield dimensions and format metadata."""
    metadata = extract_image_metadata(fixtures_dir / filename)

    assert_that(metadata.width).is_equal_to(8)
    assert_that(metadata.height).is_equal_to(6)
    assert_that(metadata.image_format).is_equal_to(expected_format)
    assert_that(metadata.bit_depth).is_equal_to(8)


def test_extract_image_metadata_detects_alpha(fixtures_dir: Path) -> None:
    """An RGBA PNG is reported as carrying an alpha channel."""
    metadata = extract_image_metadata(fixtures_dir / "sample.png")

    assert_that(metadata.color_mode).is_equal_to("RGBA")
    assert_that(metadata.has_alpha).is_true()


def test_extract_image_metadata_no_alpha_for_rgb(fixtures_dir: Path) -> None:
    """An opaque RGB JPEG is reported without an alpha channel."""
    metadata = extract_image_metadata(fixtures_dir / "sample.jpg")

    assert_that(metadata.has_alpha).is_false()


@pytest.mark.skipif(not heif_supported(), reason="pillow-heif codec unavailable")
def test_extract_image_metadata_supports_heic(fixtures_dir: Path) -> None:
    """HEIC files decode when the optional codec is present."""
    metadata = extract_image_metadata(fixtures_dir / "sample.heic")

    assert_that(metadata.width).is_equal_to(8)
    assert_that(metadata.image_format).is_equal_to("HEIF")


def test_read_exif_returns_camera_tags(fixtures_dir: Path) -> None:
    """EXIF parsing surfaces embedded camera make and model."""
    tags = read_exif(fixtures_dir / "sample.jpg")

    assert_that(tags).contains_key("Image Make")
    assert_that(tags["Image Make"]).is_equal_to("Winnow")
    assert_that(tags["Image Model"]).is_equal_to("TestCam")


def test_read_exif_empty_for_image_without_exif(fixtures_dir: Path) -> None:
    """Images without EXIF return an empty mapping rather than raising."""
    tags = read_exif(fixtures_dir / "sample.png")

    assert_that(tags).is_empty()


def test_read_exif_missing_file_degrades(tmp_path: Path) -> None:
    """A missing file yields an empty EXIF mapping instead of raising."""
    tags = read_exif(tmp_path / "missing.jpg")

    assert_that(tags).is_empty()


def test_generate_thumbnail_downscales(fixtures_dir: Path, tmp_path: Path) -> None:
    """Thumbnails fit within the bounding box and preserve aspect ratio."""
    destination = tmp_path / "thumbs" / "thumb.png"

    result = generate_thumbnail(
        fixtures_dir / "sample.png",
        destination,
        size=(4, 4),
    )

    assert_that(result.is_file()).is_true()
    with Image.open(result) as thumbnail:
        assert_that(thumbnail.width).is_less_than_or_equal_to(4)
        assert_that(thumbnail.height).is_less_than_or_equal_to(4)


def test_generate_thumbnail_rejects_corrupt(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """A corrupt source raises MediaError during thumbnail generation."""
    with pytest.raises(MediaError):
        generate_thumbnail(fixtures_dir / "corrupt.jpg", tmp_path / "thumb.png")


def test_extract_image_metadata_missing_file() -> None:
    """A missing image path raises MediaError."""
    with pytest.raises(MediaError):
        extract_image_metadata(Path("/nonexistent/photo.jpg"))


def test_extract_image_metadata_corrupt_raises(fixtures_dir: Path) -> None:
    """A corrupt image with no usable EXIF raises MediaError."""
    with pytest.raises(MediaError):
        extract_image_metadata(fixtures_dir / "corrupt.jpg")


def test_extract_image_metadata_raw_falls_back_to_exif(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAW files Pillow cannot decode recover dimensions from EXIF."""
    raw_path = tmp_path / "capture.dng"
    raw_path.write_bytes(b"not-decodable")

    monkeypatch.setattr(
        image_module,
        "read_exif",
        lambda path: {"EXIF ExifImageWidth": "6000", "EXIF ExifImageLength": "4000"},
    )

    metadata = extract_image_metadata(raw_path)

    assert_that(metadata.width).is_equal_to(6000)
    assert_that(metadata.height).is_equal_to(4000)
    assert_that(metadata.image_format).is_equal_to("DNG")


def test_unknown_mode_bit_depth_is_none() -> None:
    """An unrecognized Pillow mode yields no bit depth."""
    from winnow.media.image import _mode_bit_depth

    assert_that(_mode_bit_depth("BOGUS")).is_none()


def test_bit_depth_for_one_bit_mode(tmp_path: Path) -> None:
    """A 1-bit bilevel image reports a bit depth of one."""
    bilevel = tmp_path / "bilevel.png"
    Image.new("1", (4, 4)).save(bilevel)

    metadata = extract_image_metadata(bilevel)

    assert_that(metadata.bit_depth).is_equal_to(1)


def test_read_exif_swallows_struct_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed EXIF that raises struct.error degrades to an empty mapping."""
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not-an-image")

    def boom(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise struct.error("unpack requires a buffer")

    monkeypatch.setattr("winnow.media.image.exifread.process_file", boom)
    assert_that(read_exif(path)).is_equal_to({})


def _write_jpeg_with_exif(path: Path, tags: dict[int, str]) -> None:
    """Save a tiny JPEG carrying the given ExifIFD string tags.

    Args:
        path: Destination path for the JPEG.
        tags: Mapping of ExifIFD tag id to string value.
    """
    exif = Image.Exif()
    exif_ifd = exif.get_ifd(0x8769)
    for tag_id, value in tags.items():
        exif_ifd[tag_id] = value
    Image.new("RGB", (4, 4)).save(path, exif=exif)


def test_extract_image_metadata_reads_date_time_original(tmp_path: Path) -> None:
    """ExifIFD DateTimeOriginal populates a naive captured_at."""
    jpeg = tmp_path / "dated.jpg"
    _write_jpeg_with_exif(jpeg, {0x9003: "2024:03:01 12:34:56"})

    metadata = extract_image_metadata(jpeg)

    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


def test_extract_image_metadata_captured_at_none_without_exif(
    fixtures_dir: Path,
) -> None:
    """An image with no EXIF reports no capture time."""
    metadata = extract_image_metadata(fixtures_dir / "sample.png")

    assert_that(metadata.captured_at).is_none()


def test_extract_image_metadata_ignores_zero_date_time_original(
    tmp_path: Path,
) -> None:
    """The EXIF all-zero placeholder does not become a capture time."""
    jpeg = tmp_path / "zeroed.jpg"
    _write_jpeg_with_exif(jpeg, {0x9003: "0000:00:00 00:00:00"})

    metadata = extract_image_metadata(jpeg)

    assert_that(metadata.captured_at).is_none()


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (
            {
                "EXIF DateTimeOriginal": "2024:03:01 12:34:56",
                "Image DateTime": "2025:01:01 00:00:00",
            },
            datetime(2024, 3, 1, 12, 34, 56),
        ),
        (
            {"Image DateTime": "2025:01:01 00:00:00"},
            datetime(2025, 1, 1, 0, 0, 0),
        ),
        (
            {
                "EXIF DateTimeOriginal": "garbage",
                "Image DateTime": "2025:01:01 00:00:00",
            },
            datetime(2025, 1, 1, 0, 0, 0),
        ),
        ({}, None),
    ],
    ids=[
        "prefers_date_time_original",
        "falls_back_to_image_date_time",
        "skips_unparseable_candidate",
        "no_date_tags",
    ],
)
def test_exif_fallback_resolves_captured_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tags: dict[str, str],
    expected: datetime | None,
) -> None:
    """The EXIF-only fallback path resolves captured_at in candidate order."""
    raw_path = tmp_path / "capture.dng"
    raw_path.write_bytes(b"not-decodable")
    monkeypatch.setattr(
        image_module,
        "read_exif",
        lambda path: {"EXIF ExifImageWidth": "6000", **tags},
    )

    metadata = extract_image_metadata(raw_path)

    assert_that(metadata.captured_at).is_equal_to(expected)


def test_extract_image_metadata_reads_exif_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXIF is read a single time even when the Pillow path fails."""
    raw_path = tmp_path / "capture.dng"
    raw_path.write_bytes(b"not-decodable")
    calls: list[Path] = []

    def counting_read_exif(path: Path) -> dict[str, str]:
        calls.append(path)
        return {"EXIF ExifImageWidth": "6000"}

    monkeypatch.setattr(image_module, "read_exif", counting_read_exif)

    extract_image_metadata(raw_path)

    assert_that(calls).is_length(1)


def test_read_maker_note_tags_empty_without_maker_note(fixtures_dir: Path) -> None:
    """A JPEG with EXIF but no MakerNote yields an empty mapping."""
    assert_that(read_maker_note_tags(fixtures_dir / "sample.jpg")).is_equal_to({})


def test_read_maker_note_tags_missing_file_degrades(tmp_path: Path) -> None:
    """A missing file degrades to an empty mapping."""
    assert_that(read_maker_note_tags(tmp_path / "missing.jpg")).is_equal_to({})


def test_read_maker_note_tags_reads_apple_note(tmp_path: Path) -> None:
    """A synthetic Apple MakerNote is decoded with the prefix stripped."""
    uuid = "A1B2C3D4-E5F6-4711-8899-AABBCCDDEEFF"
    payload = uuid.encode("ascii") + b"\x00"
    entry = struct.pack(">HHII", 0x0011, 2, len(payload), 2 + 12 + 4)
    note = (
        b"Apple iOS\x00"
        + b"\x00\x01"
        + b"MM"
        + struct.pack(">H", 1)
        + entry
        + struct.pack(">I", 0)
        + payload
    )
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[ExifTags.IFD.Exif] = {0x927C: note}
    path = tmp_path / "live.jpg"
    Image.new("RGB", (8, 8)).save(path, exif=exif)

    assert_that(read_maker_note_tags(path)).is_equal_to({"Tag 0x0011": uuid})
    assert_that(read_exif(path)).does_not_contain_key("MakerNote Tag 0x0011")
