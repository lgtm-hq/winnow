"""Tests for C2PA manifest extraction."""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from winnow.media import manifest_declares_ai_source, read_c2pa_manifest

_PNG_SIGNATURE_LENGTH = 8
_PNG_IHDR_CHUNK_LENGTH = 4 + 4 + 13 + 4
_JPEG_SOI_LENGTH = 2
_JUMBF_BOX = b"\x00\x00\x00\x30jumbc2pa.claim.digitalSourceType:"


def _jumbf_payload(source_type: bytes) -> bytes:
    """Build a minimal JUMBF-looking byte string carrying a source type.

    Args:
        source_type: The ``digitalSourceType`` suffix to embed.

    Returns:
        Bytes containing ``jumb``, ``c2pa``, and ``source_type``.
    """
    return _JUMBF_BOX + source_type


def write_jpeg_with_app11(path: Path, payload: bytes) -> Path:
    """Write a JPEG whose second segment is an ``APP11`` carrying ``payload``.

    Args:
        path: Destination file path.
        payload: JUMBF bytes to place after the ``JP`` box header.

    Returns:
        The written path.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="JPEG")
    jpeg = buffer.getvalue()
    body = b"JP" + b"\x00\x01" + b"\x00\x00\x00\x01" + payload
    segment = b"\xff\xeb" + struct.pack(">H", len(body) + 2) + body
    path.write_bytes(jpeg[:_JPEG_SOI_LENGTH] + segment + jpeg[_JPEG_SOI_LENGTH:])
    return path


def write_png_with_cabx(path: Path, payload: bytes) -> Path:
    """Write a PNG with a ``caBX`` chunk carrying ``payload`` after ``IHDR``.

    Args:
        path: Destination file path.
        payload: Chunk data bytes.

    Returns:
        The written path.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PNG")
    png = buffer.getvalue()
    chunk_type = b"caBX"
    chunk = (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload))
    )
    split = _PNG_SIGNATURE_LENGTH + _PNG_IHDR_CHUNK_LENGTH
    path.write_bytes(png[:split] + chunk + png[split:])
    return path


def test_read_c2pa_manifest_returns_jpeg_app11_payload(tmp_path: Path) -> None:
    """A JUMBF/C2PA APP11 segment is returned from a JPEG."""
    payload = _jumbf_payload(b"trainedAlgorithmicMedia")
    path = write_jpeg_with_app11(tmp_path / "ai.jpg", payload)

    manifest = read_c2pa_manifest(path)

    assert_that(manifest).is_not_none()
    assert_that(manifest).contains(payload)


def test_read_c2pa_manifest_ignores_app11_without_c2pa(tmp_path: Path) -> None:
    """An APP11 segment lacking the C2PA markers does not count as a manifest."""
    path = write_jpeg_with_app11(tmp_path / "other.jpg", b"unrelated payload")

    assert_that(read_c2pa_manifest(path)).is_none()


def test_read_c2pa_manifest_returns_png_cabx_data(tmp_path: Path) -> None:
    """A caBX chunk is returned from a PNG."""
    payload = _jumbf_payload(b"compositeWithTrainedAlgorithmicMedia")
    path = write_png_with_cabx(tmp_path / "ai.png", payload)

    manifest = read_c2pa_manifest(path)

    assert_that(manifest).is_equal_to(payload)
    with Image.open(path) as image:
        assert_that(image.size).is_equal_to((8, 8))


def test_read_c2pa_manifest_returns_none_for_plain_fixtures(
    fixtures_dir: Path,
) -> None:
    """Fixtures without manifests yield None in JPEG, PNG, and other formats."""
    assert_that(read_c2pa_manifest(fixtures_dir / "sample.png")).is_none()
    assert_that(read_c2pa_manifest(fixtures_dir / "sample.jpg")).is_none()
    assert_that(read_c2pa_manifest(fixtures_dir / "sample.webp")).is_none()


def test_read_c2pa_manifest_never_raises_for_bad_files(tmp_path: Path) -> None:
    """Corrupt or misnamed files yield None instead of raising."""
    not_png = tmp_path / "fake.png"
    not_png.write_bytes(b"not a png at all")
    truncated = tmp_path / "trunc.png"
    truncated.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", 999, b"caBX"))
    missing = tmp_path / "missing.jpg"

    assert_that(read_c2pa_manifest(not_png)).is_none()
    assert_that(read_c2pa_manifest(truncated)).is_none()
    assert_that(read_c2pa_manifest(missing)).is_none()


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (_jumbf_payload(b"trainedAlgorithmicMedia"), True),
        (_jumbf_payload(b"compositeWithTrainedAlgorithmicMedia"), True),
        (_jumbf_payload(b"digitalCapture"), False),
        (b"", False),
    ],
    ids=["trained", "composite_trained", "camera_capture", "empty"],
)
def test_manifest_declares_ai_source(manifest: bytes, expected: bool) -> None:
    """Only the generative-AI source types are recognized."""
    assert_that(manifest_declares_ai_source(manifest)).is_equal_to(expected)
