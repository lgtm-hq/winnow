"""Tests for the image metadata, EXIF, and thumbnail helpers."""

from __future__ import annotations

import io
import struct
from datetime import datetime
from pathlib import Path

import exifread
import pytest
from assertpy import assert_that
from PIL import ExifTags, Image, UnidentifiedImageError

from winnow.exceptions import MediaError
from winnow.media import image as image_module
from winnow.media.image import (
    extract_image_metadata,
    generate_thumbnail,
    heif_encoding_supported,
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


@pytest.mark.parametrize("filename", ["sample.png", "sample.heic"])
def test_extract_image_metadata_captured_at_none_without_exif(
    fixtures_dir: Path,
    filename: str,
) -> None:
    """An image with no EXIF reports no capture time."""
    if filename.endswith(".heic") and not heif_supported():
        pytest.skip("pillow-heif not available")

    metadata = extract_image_metadata(fixtures_dir / filename)

    assert_that(metadata.captured_at).is_none()


def _require_dated(directory: Path, filename: str) -> Path:
    """Return a dated fixture path, skipping when the HEIC could not be built.

    Args:
        directory: Directory expected to hold the dated fixtures.
        filename: Fixture file name.

    Returns:
        Path to the fixture.
    """
    path = directory / filename
    if not path.exists():
        pytest.skip(f"{filename} unavailable: pillow-heif cannot encode HEIF here")
    return path


@pytest.mark.parametrize("filename", ["dated.heic", "dated.jpg"])
def test_extract_image_metadata_dated_fixtures_round_trip(
    dated_images_dir: Path,
    filename: str,
) -> None:
    """Dated fixtures built at test time read DateTimeOriginal via Pillow."""
    path = _require_dated(dated_images_dir, filename)

    metadata = extract_image_metadata(path)

    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


@pytest.mark.parametrize("filename", ["dated.heic", "dated.jpg"])
def test_extract_image_metadata_committed_dated_fixtures(
    fixtures_dir: Path,
    filename: str,
) -> None:
    """The committed dated fixtures carry the expected capture date."""
    if filename.endswith(".heic") and not heif_supported():
        pytest.skip("pillow-heif not available")

    metadata = extract_image_metadata(fixtures_dir / filename)

    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


def test_extract_image_metadata_falls_back_to_ifd0_date_time(tmp_path: Path) -> None:
    """IFD0 DateTime is used when the ExifIFD has no DateTimeOriginal."""
    jpeg = tmp_path / "ifd0.jpg"
    exif = Image.Exif()
    exif[0x0132] = "2024:03:02 00:00:00"
    Image.new("RGB", (4, 4)).save(jpeg, exif=exif)

    metadata = extract_image_metadata(jpeg)

    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 2, 0, 0, 0))


def test_extract_image_metadata_reads_ifd0_date_time_when_exif_ifd_is_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed ExifIFD does not hide a still-valid IFD0 DateTime."""
    jpeg = tmp_path / "broken-ifd.jpg"
    exif = Image.Exif()
    exif[0x0132] = "2024:03:03 00:00:00"
    Image.new("RGB", (4, 4)).save(jpeg, exif=exif)

    def _broken_get_ifd(self: Image.Exif, tag: int) -> dict[int, object]:
        raise SyntaxError("corrupt ExifIFD")

    monkeypatch.setattr(Image.Exif, "get_ifd", _broken_get_ifd)

    metadata = extract_image_metadata(jpeg)

    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 3, 0, 0, 0))


def test_extract_image_metadata_falls_back_when_date_time_original_invalid(
    tmp_path: Path,
) -> None:
    """A malformed DateTimeOriginal does not block the IFD0 DateTime fallback."""
    jpeg = tmp_path / "bad-original.jpg"
    exif = Image.Exif()
    exif[0x0132] = "2024:03:02 00:00:00"
    exif.get_ifd(0x8769)[0x9003] = "0000:00:00 00:00:00"
    Image.new("RGB", (4, 4)).save(jpeg, exif=exif)

    metadata = extract_image_metadata(jpeg)

    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 2, 0, 0, 0))


def test_extract_image_metadata_skips_exifread_for_decodable_images(
    dated_images_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Images Pillow can open never touch exifread."""
    path = _require_dated(dated_images_dir, "dated.heic")
    calls: list[Path] = []

    def counting_read_exif(path: Path) -> dict[str, str]:
        calls.append(path)
        return {}

    monkeypatch.setattr(image_module, "read_exif", counting_read_exif)

    metadata = extract_image_metadata(path)

    assert_that(calls).is_empty()
    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


def test_extract_image_metadata_uses_exifread_when_pillow_cannot_open(
    dated_images_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Pillow cannot identify a file, metadata comes from the EXIF tags."""
    path = _require_dated(dated_images_dir, "dated.jpg")

    def failing_open(*args: object, **kwargs: object) -> None:
        raise UnidentifiedImageError("cannot identify")

    monkeypatch.setattr(
        image_module,
        "read_exif",
        lambda path: {
            "EXIF ExifImageWidth": "32",
            "EXIF ExifImageLength": "24",
            "EXIF DateTimeOriginal": "2024:03:01 12:34:56",
        },
    )
    monkeypatch.setattr(Image, "open", failing_open)

    metadata = extract_image_metadata(path)

    assert_that(metadata.width).is_equal_to(32)
    assert_that(metadata.height).is_equal_to(24)
    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))


def test_read_exif_dated_heic_yields_no_capture_date(dated_images_dir: Path) -> None:
    """exifread cannot supply a HEIC capture date; the mapping lacks the tag."""
    path = _require_dated(dated_images_dir, "dated.heic")

    tags = read_exif(path)

    assert_that(tags).does_not_contain_key("EXIF DateTimeOriginal")
    assert_that(tags).does_not_contain_key("Image DateTime")


def test_extract_image_metadata_swallows_malformed_exif(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed EXIF block yields dimensions with no capture time."""

    def _broken_getexif(self: Image.Image) -> Image.Exif:
        raise SyntaxError("not a TIFF header")

    monkeypatch.setattr(Image.Image, "getexif", _broken_getexif)

    metadata = extract_image_metadata(fixtures_dir / "sample.jpg")

    assert_that(metadata.width).is_equal_to(8)
    assert_that(metadata.height).is_equal_to(6)
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


def _apple_maker_note(uuid: str) -> bytes:
    """Build an Apple MakerNote blob carrying a content identifier.

    Mirrors the real layout: ``Apple iOS\\0`` + 2-byte version, ``MM`` byte
    order, then a TIFF IFD at offset 14 with one ASCII entry (``Tag 0x0011``)
    whose value offset is relative to the start of the blob.

    Args:
        uuid: ASCII identifier stored in ``Tag 0x0011``.

    Returns:
        Raw MakerNote bytes suitable for EXIF tag ``0x927C``.
    """
    payload = uuid.encode("ascii") + b"\x00"
    value_offset = 14 + 2 + 12 + 4
    entry = struct.pack(">HHII", 0x0011, 2, len(payload), value_offset)
    ifd = struct.pack(">H", 1) + entry + struct.pack(">I", 0)
    return b"Apple iOS\x00" + b"\x00\x01" + b"MM" + ifd + payload


def _apple_exif(note: bytes | None) -> Image.Exif:
    """Build an EXIF block for an Apple still, optionally with a MakerNote.

    Args:
        note: Raw MakerNote bytes, or ``None`` to omit the tag.

    Returns:
        EXIF block ready to pass to ``Image.save``.
    """
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[ExifTags.IFD.Exif] = {0x927C: note} if note is not None else {}
    return exif


def test_read_maker_note_tags_reads_apple_note(tmp_path: Path) -> None:
    """A synthetic Apple MakerNote JPEG is decoded with the prefix stripped."""
    uuid = "A1B2C3D4-E5F6-4711-8899-AABBCCDDEEFF"
    path = tmp_path / "live.jpg"
    Image.new("RGB", (8, 8)).save(path, exif=_apple_exif(_apple_maker_note(uuid)))

    assert_that(read_maker_note_tags(path)).is_equal_to({"Tag 0x0011": uuid})
    assert_that(read_exif(path)).does_not_contain_key("MakerNote Tag 0x0011")


@pytest.mark.skipif(
    not heif_encoding_supported(),
    reason="pillow-heif HEIF encoder unavailable",
)
def test_read_maker_note_tags_reads_apple_note_from_heic(tmp_path: Path) -> None:
    """A HEIC still carrying an Apple MakerNote yields the content identifier."""
    uuid = "A1B2C3D4-E5F6-4711-8899-AABBCCDDEEFF"
    path = tmp_path / "live.heic"
    exif = _apple_exif(_apple_maker_note(uuid))
    Image.new("RGB", (8, 8)).save(path, format="HEIF", exif=exif.tobytes())

    assert_that(read_maker_note_tags(path)["Tag 0x0011"]).is_equal_to(uuid)


@pytest.mark.skipif(
    not heif_encoding_supported(),
    reason="pillow-heif HEIF encoder unavailable",
)
def test_read_maker_note_tags_empty_for_heic_without_maker_note(
    tmp_path: Path,
) -> None:
    """A HEIC with EXIF but no MakerNote yields an empty mapping."""
    path = tmp_path / "plain.heic"
    Image.new("RGB", (8, 8)).save(path, format="HEIF", exif=_apple_exif(None).tobytes())

    assert_that(read_maker_note_tags(path)).is_equal_to({})


@pytest.mark.parametrize(
    "note",
    [
        b"Apple iOS\x00\x00\x01MM\x00",
        b"Apple iOS\x00\x00\x01MM" + struct.pack(">H", 1),
        _apple_maker_note("A1B2C3D4-E5F6-4711-8899-AABBCCDDEEFF")[:-8],
        b"Apple iOS\x00\x00\x01XX" + struct.pack(">H", 0),
    ],
    ids=[
        "truncated_count",
        "truncated_entry",
        "truncated_value",
        "unknown_byte_order",
    ],
)
def test_read_maker_note_tags_empty_for_malformed_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    note: bytes,
) -> None:
    """Truncated or foreign MakerNote bytes degrade to an empty mapping.

    exifread is stubbed to a distinctive rescue value so the assertion proves
    the Pillow parser, not the fallback, produced the empty result.
    """
    monkeypatch.setattr(
        exifread,
        "process_file",
        lambda *a, **k: {"MakerNote Tag 0x0011": "EXIFREAD-RESCUE"},
    )
    path = tmp_path / "broken.jpg"
    Image.new("RGB", (8, 8)).save(path, exif=_apple_exif(note))

    assert_that(read_maker_note_tags(path)).is_equal_to({})


def test_non_apple_maker_note_uses_exifread_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MakerNote from another vendor is left to the exifread reader."""
    monkeypatch.setattr(
        exifread,
        "process_file",
        lambda *a, **k: {"MakerNote Tag 0x0011": "EXIFREAD-RESCUE"},
    )
    path = tmp_path / "canon.jpg"
    note = b"Canon\x00\x00\x01MM" + struct.pack(">H", 0)
    Image.new("RGB", (8, 8)).save(path, exif=_apple_exif(note))

    assert_that(read_maker_note_tags(path)).is_equal_to(
        {"Tag 0x0011": "EXIFREAD-RESCUE"},
    )


def test_malformed_apple_note_never_falls_back_to_exifread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recognised but malformed Apple MakerNote is authoritative.

    exifread mis-reads Apple value offsets and can return a truncated
    ``Tag 0x0011`` for the same bytes, which would create a false pairing.
    """
    calls: list[str] = []

    def _fake_exifread(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("exifread")
        return {"MakerNote Tag 0x0011": "GARB"}

    monkeypatch.setattr(exifread, "process_file", _fake_exifread)
    path = tmp_path / "truncated.jpg"
    Image.new("RGB", (8, 8)).save(
        path, exif=_apple_exif(b"Apple iOS\x00\x00\x01MM\x00")
    )

    assert_that(read_maker_note_tags(path)).is_equal_to({})
    assert_that(calls).is_empty()


def test_heif_encoding_supported_matches_a_real_encode() -> None:
    """The encoder probe agrees with an actual in-memory HEIF save."""
    expected = False
    if heif_supported():
        try:
            Image.new("RGB", (2, 2)).save(io.BytesIO(), format="HEIF")
            expected = True
        except Exception:  # noqa: BLE001 - probing the codec
            expected = False

    assert_that(heif_encoding_supported()).is_equal_to(expected)


@pytest.mark.parametrize(
    ("order", "endian"),
    [(b"MM", ">"), (b"II", "<")],
    ids=["big_endian", "little_endian"],
)
def test_read_maker_note_tags_skips_non_string_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: bytes,
    endian: str,
) -> None:
    """Inline values decode in either byte order; non-string entries are skipped."""
    monkeypatch.setattr(
        exifread,
        "process_file",
        lambda *a, **k: {"MakerNote Tag 0x0011": "EXIFREAD-RESCUE"},
    )
    entries = (
        struct.pack(f"{endian}HHI", 0x0001, 9, 1) + struct.pack(f"{endian}I", 7),
        struct.pack(f"{endian}HHI", 0x0003, 7, 4) + b"\xff\xfe\x00\x01",
        struct.pack(f"{endian}HHI", 0x0011, 2, 4) + b"abc\x00",
    )
    note = (
        b"Apple iOS\x00\x00\x01"
        + order
        + struct.pack(f"{endian}H", len(entries))
        + b"".join(entries)
        + struct.pack(f"{endian}I", 0)
    )
    path = tmp_path / "inline.jpg"
    Image.new("RGB", (8, 8)).save(path, exif=_apple_exif(note))

    assert_that(read_maker_note_tags(path)).is_equal_to({"Tag 0x0011": "abc"})


def test_read_maker_note_tags_falls_back_to_exifread(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Pillow finds no Apple MakerNote, exifread's MakerNote keys are used."""
    monkeypatch.setattr(
        exifread,
        "process_file",
        lambda handle, details: (
            {"MakerNote Tag 0x0011": "from-exifread", "Image Make": "X"}
            if details
            else {}
        ),
    )

    tags = read_maker_note_tags(fixtures_dir / "sample.jpg")

    assert_that(tags).is_equal_to({"Tag 0x0011": "from-exifread"})


def test_read_maker_note_tags_swallows_unexpected_pillow_error(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlisted codec exception from Pillow degrades to the exifread path."""

    def _boom(*args: object, **kwargs: object) -> Image.Image:
        raise RuntimeError("codec exploded")

    monkeypatch.setattr(Image, "open", _boom)
    monkeypatch.setattr(exifread, "process_file", lambda *a, **k: {})

    assert_that(read_maker_note_tags(fixtures_dir / "sample.jpg")).is_equal_to({})
