"""Tests for the bounded JPEG segment walk and the IPTC IIM splitter."""

from __future__ import annotations

import io
import struct

import pytest
from assertpy import assert_that

from winnow.media.readers._segments import (
    MAX_SEGMENTS,
    IimDataset,
    JpegMetadata,
    iim_datasets,
    scan_jpeg,
)

_SOI = b"\xff\xd8"
_EXIF = b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08"


def _segment(code: int, payload: bytes) -> bytes:
    """Encode one marker segment.

    Args:
        code: The marker code.
        payload: Its payload.

    Returns:
        The marker, its length and the payload.
    """
    return bytes([0xFF, code]) + struct.pack(">H", len(payload) + 2) + payload


def _resource(resource: int, data: bytes, *, size: int | None = None) -> bytes:
    """Encode one Photoshop image resource with an empty name.

    Args:
        resource: The resource id.
        data: Its data.
        size: The declared size, when it should differ from the data's.

    Returns:
        The resource block, padded to an even length.
    """
    declared = len(data) if size is None else size
    header = b"8BIM" + struct.pack(">H", resource) + b"\x00\x00"
    return header + struct.pack(">I", declared) + data + b"\x00" * (len(data) % 2)


def _app13(*resources: bytes) -> bytes:
    """Encode a Photoshop ``APP13`` segment.

    Args:
        resources: Its resource blocks.

    Returns:
        The segment.
    """
    return _segment(0xED, b"Photoshop 3.0\x00" + b"".join(resources))


def _scan(data: bytes) -> JpegMetadata:
    """Scan JPEG bytes.

    Args:
        data: The bytes.

    Returns:
        What the scan found.
    """
    return scan_jpeg(io.BytesIO(data))


def test_scan_jpeg_collects_exif_and_iptc() -> None:
    """The first EXIF APP1 and every IPTC resource are returned."""
    data = (
        _SOI
        + _segment(0xE0, b"JFIF\x00")
        + _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x/>")
        + _segment(0xE1, _EXIF)
        + _segment(0xE1, _EXIF + b"second")
        + _app13(_resource(0x0425, b"digest"), _resource(0x0404, b"one"))
        + _app13(_resource(0x0404, b"two!"))
        + b"\xff\xff\xd0"
        + _segment(0xDA, b"scan")
        + _segment(0xE1, _EXIF + b"after-scan")
    )

    assert_that(_scan(data)).is_equal_to(
        JpegMetadata(exif=_EXIF, iptc=(b"one", b"two!")),
    )


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"\x89PNG", "no start-of-image"),
        (_SOI + b"\x00\xe1", "no marker at offset 2"),
        (_SOI + b"\xff\xe1\x00", "has no length"),
        (_SOI + b"\xff\xe1\x00\x01", "declares length 1"),
        (_SOI + b"\xff\xe1\x00\x10Exif", "declares 14 bytes, only 4 remain"),
        (_SOI + b"\xff\xe0\x00\x10JFIF", "runs past the end"),
    ],
    ids=[
        "not_jpeg",
        "no_marker",
        "no_length",
        "short_length",
        "kept_segment_cut",
        "skipped_segment_cut",
    ],
)
def test_scan_jpeg_reports_damage(data: bytes, problem: str) -> None:
    """Damaged segments are reported, not read as absent."""
    found = _scan(data)

    assert_that(found.problems).is_length(1)
    assert_that(found.problems[0]).contains(problem)


def test_scan_jpeg_without_image_data_is_not_damage() -> None:
    """A file that ends after its metadata segments reports nothing."""
    found = _scan(_SOI + _segment(0xE1, _EXIF))

    assert_that(found).is_equal_to(JpegMetadata(exif=_EXIF))


def test_scan_jpeg_stops_after_the_segment_limit() -> None:
    """The walk stops after the segment limit, so a crafted file is bounded."""
    data = _SOI + _segment(0xE0, b"") * MAX_SEGMENTS + _segment(0xE1, _EXIF)

    assert_that(_scan(data)).is_equal_to(JpegMetadata())


def test_scan_jpeg_skips_standalone_markers() -> None:
    """Restart and TEM markers carry no length and are skipped."""
    data = _SOI + b"\xff\x01\xff\xd3" + _segment(0xE1, _EXIF)

    assert_that(_scan(data).exif).is_equal_to(_EXIF)


@pytest.mark.parametrize(
    ("resources", "kept", "problem"),
    [
        (b"MeSa" + b"\x00" * 12, (), "no resource signature"),
        (b"8BIM\x04", (), "header cut short"),
        (b"8BIM\x04\x04\x00\x00\x00", (), "size cut short"),
        (_resource(0x0404, b"abc", size=9)[:-1], (b"abc",), "declares 9 bytes"),
    ],
    ids=["signature", "header", "size", "data"],
)
def test_scan_jpeg_reports_damaged_photoshop_resources(
    resources: bytes,
    kept: tuple[bytes, ...],
    problem: str,
) -> None:
    """A damaged resource is reported; a cut IPTC resource is kept as far as it goes."""
    found = _scan(_SOI + _app13(resources))

    assert_that(found.iptc).is_equal_to(kept)
    assert_that(found.problems).is_length(1)
    assert_that(found.problems[0]).contains(problem)


def test_scan_jpeg_accepts_trailing_resource_padding() -> None:
    """NUL padding after the last resource is not damage."""
    found = _scan(_SOI + _app13(_resource(0x0404, b"ab"), b"\x00\x00"))

    assert_that(found).is_equal_to(JpegMetadata(iptc=(b"ab",)))


def _dataset(record: int, dataset: int, value: bytes) -> bytes:
    """Encode one IIM dataset.

    Args:
        record: The record number.
        dataset: The dataset number.
        value: Its bytes.

    Returns:
        The encoded dataset.
    """
    return bytes([0x1C, record, dataset]) + struct.pack(">H", len(value)) + value


def test_iim_datasets_splits_every_dataset() -> None:
    """Datasets come back in order, empty ones and NUL padding included."""
    data = (
        _dataset(1, 90, b"\x1b%G")
        + _dataset(2, 55, b"")
        + _dataset(2, 60, b"1")
        + b"\0"
    )

    assert_that(iim_datasets(data)).is_equal_to(
        (
            [
                IimDataset(1, 90, b"\x1b%G"),
                IimDataset(2, 55, b""),
                IimDataset(2, 60, b"1"),
            ],
            None,
        ),
    )


@pytest.mark.parametrize(
    ("tail", "problem"),
    [
        (b"\x1c\x02\x3c\x00", "header cut short at offset 13"),
        (
            _dataset(2, 60, b"101010+0900")[:-1],
            "dataset 2:60 declares 11 bytes, only 10",
        ),
        (b"\x1c\x02\x3c\x80\x04abcd", "dataset 2:60 uses an extended length"),
        (b"\x00junk", "no dataset tag at offset 13"),
    ],
    ids=["header", "overstated", "extended", "junk"],
)
def test_iim_datasets_keeps_datasets_before_damage(tail: bytes, problem: str) -> None:
    """Every complete dataset before the damage is kept and the damage named."""
    datasets, damage = iim_datasets(_dataset(2, 55, b"20180704") + tail)

    assert_that(datasets).is_equal_to([IimDataset(2, 55, b"20180704")])
    assert_that(str(damage)).contains(problem)
