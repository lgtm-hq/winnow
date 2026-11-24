"""Tests for the bounded JPEG, TIFF and IPTC IIM fallback readers."""

from __future__ import annotations

import io
import struct

import pytest
from assertpy import assert_that

from tests.media.detect_support import cpu_seconds
from winnow.media.readers._segments import (
    MAX_GAP,
    MAX_GAPS,
    MAX_SEGMENTS,
    SCAN_BUDGET,
    IimDataset,
    JpegMetadata,
    TiffTagBytes,
    iim_datasets,
    read_tiff_tags,
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
        (_SOI + b"\xff\xe1\x00", "has no length"),
        (_SOI + b"\xff\xe1\x00\x01", "declares length 1"),
        (_SOI + b"\xff\xe1\x00\x10Exif", "declares 14 bytes, only 4 remain"),
        (_SOI + b"\xff\xe0\x00\x10JFIF", "runs past the end"),
    ],
    ids=[
        "not_jpeg",
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

    assert_that(_scan(data)).is_equal_to(
        JpegMetadata(problems=(f"JPEG: scan stopped after {MAX_SEGMENTS} segments",)),
    )


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
        (b"\x00junk", "no dataset tag at offset 13"),
    ],
    ids=["header", "overstated", "junk"],
)
def test_iim_datasets_keeps_datasets_before_damage(tail: bytes, problem: str) -> None:
    """Every complete dataset before the damage is kept and the damage named."""
    datasets, damage = iim_datasets(_dataset(2, 55, b"20180704") + tail)

    assert_that(datasets).is_equal_to([IimDataset(2, 55, b"20180704")])
    assert_that(str(damage)).contains(problem)


@pytest.mark.parametrize(
    ("gap", "skipped"),
    [(b"\x00\x00\x00", 3), (b"\xff\x00", 2), (b"\x00\xff\x00junk", 7)],
    ids=["nul_bytes", "stray_ff00", "mixed"],
)
def test_scan_jpeg_resynchronises_after_a_gap(gap: bytes, skipped: int) -> None:
    """Stray bytes between segments are reported and the walk goes on."""
    data = (
        _SOI
        + _segment(0xE0, b"JFIF\x00")
        + gap
        + _segment(0xE1, _EXIF)
        + _app13(_resource(0x0404, b"iptc"))
    )

    found = _scan(data)

    assert_that((found.exif, found.iptc)).is_equal_to((_EXIF, (b"iptc",)))
    assert_that(found.problems).is_length(1)
    assert_that(found.problems[0]).contains(f"{skipped} stray bytes skipped")


def test_scan_jpeg_stops_after_too_many_gaps() -> None:
    """More than the allowed number of gaps stops the walk, reported."""
    data = (
        _SOI + (b"\x00" + _segment(0xE0, b"")) * (MAX_GAPS + 1) + _segment(0xE1, _EXIF)
    )

    found = _scan(data)

    assert_that(found.exif).is_none()
    assert_that(found.problems[-1]).contains(f"stopped after {MAX_GAPS} gaps")


def test_scan_jpeg_gives_up_on_a_long_gap() -> None:
    """No marker within the gap limit ends the walk, reported."""
    found = _scan(_SOI + b"\x00" * (MAX_GAP + 10) + _segment(0xE1, _EXIF))

    assert_that(found.exif).is_none()
    assert_that(found.problems).is_equal_to(
        (f"JPEG: no marker within {MAX_GAP} bytes of offset 2",),
    )


def test_scan_jpeg_fill_run_is_cpu_bounded() -> None:
    """A 16 MiB run of fill bytes is searched in blocks, within a CPU bound."""
    data = _SOI + b"\xff" * (16 * 1024 * 1024) + _segment(0xE1, _EXIF)

    found, elapsed = cpu_seconds(lambda: _scan(data))

    assert_that(elapsed).is_less_than(0.5)
    assert_that(found.problems).is_length(1)
    assert_that(found.problems[0]).contains(f"no marker within {MAX_GAP} bytes")


def test_scan_jpeg_fill_bytes_before_a_marker_are_not_damage() -> None:
    """A few fill bytes before a marker, across a block boundary, are legal."""
    data = _SOI + _segment(0xE0, b"x" * 4085) + b"\xff" * 20 + _segment(0xE1, _EXIF)

    assert_that(_scan(data)).is_equal_to(JpegMetadata(exif=_EXIF))


def test_scan_jpeg_stops_at_the_byte_budget() -> None:
    """A scan that reaches the byte budget stops and says so."""
    full = _segment(0xE2, b"x" * 65533)
    data = _SOI + full * (SCAN_BUDGET // len(full) + 1) + _segment(0xE1, _EXIF)

    found = _scan(data)

    assert_that(found.exif).is_none()
    assert_that(found.problems[-1]).contains(f"stopped after {SCAN_BUDGET} bytes")


def test_scan_jpeg_keeps_a_cut_short_iptc_segment() -> None:
    """The bytes of an APP13 cut short are returned with the damage."""
    iim = _dataset(2, 55, b"20180704") + _dataset(2, 120, b"c" * 40)
    data = _SOI + _app13(_resource(0x0404, iim))

    found = _scan(data[:-20])

    assert_that(found.iptc).is_length(1)
    assert_that(iim_datasets(found.iptc[0])[0]).is_equal_to(
        [IimDataset(2, 55, b"20180704")],
    )
    assert_that([p.split(" declares")[0] for p in found.problems]).is_equal_to(
        ["APP13: resource 0x0404", "JPEG: segment 0xED"],
    )


def test_iim_datasets_reads_extended_lengths() -> None:
    """An extended-length dataset is valid and the datasets after it are read."""
    preview = b"p" * 40000
    extended = bytes([0x1C, 2, 202, 0x80, 0x04]) + struct.pack(">I", len(preview))
    short = bytes([0x1C, 2, 60, 0x80, 0x02]) + struct.pack(">H", 11) + b"101010+0900"
    data = extended + preview + short + _dataset(2, 55, b"20180704")

    assert_that(iim_datasets(data)).is_equal_to(
        (
            [
                IimDataset(2, 202, preview),
                IimDataset(2, 60, b"101010+0900"),
                IimDataset(2, 55, b"20180704"),
            ],
            None,
        ),
    )


@pytest.mark.parametrize(
    ("tail", "problem"),
    [
        (b"\x1c\x02\x3c\x80\x00", "declares a 0-byte extended length"),
        (b"\x1c\x02\x3c\x80\x05" + b"\x00" * 5, "declares a 5-byte extended length"),
        (b"\x1c\x02\x3c\x80\x04\x00", "extended length cut short"),
        (b"\x1c\x02\x3c\x80\x01\x09abc", "declares 9 bytes, only 3 remain"),
    ],
    ids=["zero_size", "too_wide", "length_cut", "value_cut"],
)
def test_iim_datasets_reports_damaged_extended_lengths(
    tail: bytes, problem: str
) -> None:
    """A damaged extended length ends the block; earlier datasets are kept."""
    datasets, damage = iim_datasets(_dataset(2, 55, b"20180704") + tail)

    assert_that(datasets).is_equal_to([IimDataset(2, 55, b"20180704")])
    assert_that(str(damage)).contains(problem)


def _tiff(entries: list[tuple[int, int, int, bytes]], *, big: bool = False) -> bytes:
    """Build a little-endian TIFF whose IFD0 holds the given entries.

    Values longer than the inline field are stored after the directory.

    Args:
        entries: ``(tag, type, count, value bytes)`` in tag order.
        big: Whether to write BigTIFF.

    Returns:
        The file's bytes.
    """
    header = (
        b"II+\x00\x08\x00\x00\x00" + struct.pack("<Q", 16)
        if big
        else b"II*\x00" + struct.pack("<I", 8)
    )
    count_format, entry_size, inline = ("<Q", 20, 8) if big else ("<H", 12, 4)
    directory_end = (
        len(header) + struct.calcsize(count_format) + entry_size * len(entries) + inline
    )
    fields, extra = [], b""
    for tag, kind, count, value in entries:
        if len(value) <= inline:
            field = value.ljust(inline, b"\x00")
        else:
            offset = directory_end + len(extra)
            field = struct.pack("<Q" if big else "<I", offset)
            extra += value
        fields.append(
            struct.pack("<HH", tag, kind)
            + struct.pack("<Q" if big else "<I", count)
            + field
        )
    body = struct.pack(count_format, len(entries)) + b"".join(fields) + b"\x00" * inline
    return header + body + extra


def _tiff_with_note(note: bytes, *, iptc: bytes = b"") -> bytes:
    """Build a TIFF with IPTC in IFD0 and a MakerNote in its ExifIFD.

    Args:
        note: The MakerNote bytes.
        iptc: The IPTC-NAA bytes; omitted when empty.

    Returns:
        The file's bytes.
    """
    exif_ifd = struct.pack("<H", 1) + struct.pack("<HHI", 0x927C, 7, len(note))
    head_entries = 2 if iptc else 1
    ifd0_size = 2 + 12 * head_entries + 4
    exif_at = 8 + ifd0_size + len(iptc)
    note_at = exif_at + len(exif_ifd) + 4 + 4
    exif_ifd += struct.pack("<I", note_at) + b"\x00" * 4
    entries = b""
    if iptc:
        entries += struct.pack("<HHII", 33723, 7, len(iptc), 8 + ifd0_size)
    entries += struct.pack("<HHII", 0x8769, 4, 1, exif_at)
    ifd0 = struct.pack("<H", head_entries) + entries + b"\x00" * 4
    return b"II*\x00" + struct.pack("<I", 8) + ifd0 + iptc + exif_ifd + note


@pytest.mark.parametrize("big", [False, True], ids=["classic", "bigtiff"])
def test_read_tiff_tags_reads_iptc_exactly(big: bool) -> None:
    """The IPTC-NAA tag's bytes come back exactly, whatever its type."""
    iptc = _dataset(2, 55, b"20180704") + _dataset(2, 120, b"c" * 1500)
    iptc += b"\x00" * (-len(iptc) % 4)
    data = _tiff([(33723, 4, len(iptc) // 4, iptc)], big=big)

    assert_that(read_tiff_tags(io.BytesIO(data))).is_equal_to(TiffTagBytes(iptc=iptc))


def test_read_tiff_tags_reads_an_inline_value() -> None:
    """A value that fits the entry's field is read from it."""
    data = _tiff([(33723, 7, 3, b"abc")])

    assert_that(read_tiff_tags(io.BytesIO(data)).iptc).is_equal_to(b"abc")


def test_read_tiff_tags_reads_the_maker_note() -> None:
    """The ExifIFD MakerNote's bytes come back exactly."""
    note = b"Apple iOS\x00" + b"n" * 60

    found = read_tiff_tags(io.BytesIO(_tiff_with_note(note, iptc=b"iptc-block")))

    assert_that(found).is_equal_to(TiffTagBytes(iptc=b"iptc-block", maker_note=note))


def test_read_tiff_tags_reports_a_cut_maker_note() -> None:
    """A MakerNote the file does not hold completely is not returned."""
    data = _tiff_with_note(b"Apple iOS\x00" + b"n" * 60)

    found = read_tiff_tags(io.BytesIO(data[:-5]))

    assert_that(found.maker_note).is_none()
    assert_that(found.problems).is_length(1)
    assert_that(found.problems[0]).contains("MakerNote declares 70 bytes")


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"XX*\x00\x08\x00\x00\x00", "no byte-order mark"),
        (b"II\x2b\x01\x08\x00\x00\x00", "unknown version"),
        (b"II*\x00\x08\x00\x00\x00\x01", "directory at 8 cut short"),
        (b"II*\x00\x08\x00\x00\x00" + struct.pack("<H", 5000), "declares 5000 entries"),
        (b"II*\x00\x08\x00\x00\x00\x01\x00" + b"\x00" * 4, "directory at 8 cut short"),
        (_tiff([(33723, 99, 1, b"a")]), "IPTC-NAA has unknown type 99"),
        (_tiff([(33723, 7, SCAN_BUDGET + 1, b"abcde")]), "exceeds the read budget"),
    ],
    ids=[
        "byte_order",
        "version",
        "no_count",
        "too_many_entries",
        "entries_cut",
        "unknown_type",
        "over_budget",
    ],
)
def test_read_tiff_tags_reports_damage(data: bytes, problem: str) -> None:
    """Damaged TIFF structure is reported, never read as absent tags."""
    found = read_tiff_tags(io.BytesIO(data))

    assert_that(found.problems).is_length(1)
    assert_that(found.problems[0]).contains(problem)
