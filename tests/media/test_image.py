"""Tests for the image metadata, EXIF, and thumbnail helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from winnow.exceptions import MediaError
from winnow.media import image as image_module
from winnow.media.image import (
    extract_image_metadata,
    generate_thumbnail,
    heif_supported,
    read_exif,
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


def test_bit_depth_for_one_bit_mode(tmp_path: Path) -> None:
    """A 1-bit bilevel image reports a bit depth of one."""
    bilevel = tmp_path / "bilevel.png"
    Image.new("1", (4, 4)).save(bilevel)

    metadata = extract_image_metadata(bilevel)

    assert_that(metadata.bit_depth).is_equal_to(1)
