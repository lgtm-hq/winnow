"""Edge cases of the detection walkers and frame checks (#393).

Each case is a tiny literal structure; it asserts what the walker or check
returns for a truncated, foreign or unusual input.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.signature_support import (
    VORBIS_ID,
    id3v2,
    mpeg_frames,
    ogg_page,
    program_stream,
)
from winnow.media._audio_frames import (
    ac3_frame_length,
    adts_frame_length,
    mpeg_audio_frame_length,
    skip_id3v2,
)
from winnow.media._containers import (
    ASF_HEADER,
    AUDIO,
    asf_streams,
    ogg_streams,
    program_stream_streams,
    realmedia_streams,
)
from winnow.media._detect import detect
from winnow.media._detect_base import MalformedError, Reader
from winnow.models.media import MediaType

# --- frame headers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "length"),
    [
        (b"\xff\xff\x90\x00", 312),  # MPEG-1 layer I, 288 kbit/s, 44.1 kHz
        (b"\xff\xf3\x90\x00", 261),  # MPEG-2 layer III, 80 kbit/s, 22.05 kHz
        (b"\xff\xe3\x90\x00", 522),  # MPEG-2.5 layer III, 80 kbit/s, 11.025 kHz
        (b"\xff\xfd\x90\x00", 522),  # MPEG-1 layer II, 160 kbit/s, 44.1 kHz
        (b"\xff\xfb\x92\x00", 418),  # padded
    ],
    ids=["layer1", "mpeg2-layer3", "mpeg25-layer3", "layer2", "padded"],
)
def test_mpeg_audio_frame_lengths(header: bytes, length: int) -> None:
    """Frame lengths follow ISO/IEC 11172-3 and 13818-3."""
    assert_that(mpeg_audio_frame_length(header)).is_equal_to(length)


@pytest.mark.parametrize(
    "header",
    [
        b"\xff\xfb\x90",  # short
        b"\xff\xeb\x90\x00",  # reserved version
        b"\xff\xf9\x90\x00",  # reserved layer
        b"\xff\xfb\x9c\x00",  # reserved sample rate
        b"\xff\xfb\x00\x00",  # free format
        b"\xff\xfb\x90\x02",  # reserved emphasis
    ],
)
def test_invalid_mpeg_audio_headers(header: bytes) -> None:
    """Reserved and unsupported field values are rejected."""
    assert_that(mpeg_audio_frame_length(header)).is_none()


def test_adts_rejects_reserved_sample_rate() -> None:
    """An ADTS sampling frequency index above 12 is invalid."""
    assert_that(adts_frame_length(b"\xff\xf1\x74\x80\x08\x1f\xfc")).is_none()


@pytest.mark.parametrize(
    "header",
    [
        b"\x0b\x77\x00\x00\x10\x88",  # bsid 17
        b"\x0b\x77\xc0\x7f\x3f\x80",  # E-AC-3 stream type 3
        b"\x0b\x77\x00\x7f\xff\x80",  # E-AC-3 fscod and fscod2 both 3
        b"\x0b\x77\x00\x00\x3f\x40",  # AC-3 frmsizecod 63
    ],
    ids=["bsid-17", "strmtyp-3", "fscod2-3", "frmsizecod-63"],
)
def test_invalid_ac3_headers(header: bytes) -> None:
    """Reserved AC-3 and E-AC-3 values are rejected."""
    assert_that(ac3_frame_length(header)).is_none()


def test_ac3_44k_frame_length() -> None:
    """AC-3 at 44.1 kHz uses the rounded table length plus the odd code."""
    assert_that(ac3_frame_length(b"\x0b\x77\x00\x00\x51\x40")).is_equal_to(558)


@pytest.mark.parametrize(
    "tag",
    [
        b"ID3\x05\x00\x00\x00\x00\x00\x00",
        b"ID3\x03\xff\x00\x00\x00\x00\x00",
        b"ID3\x03\x00\x00\x80\x00\x00\x00",
    ],
    ids=["version-5", "revision-ff", "not-synchsafe"],
)
def test_invalid_id3_headers(tag: bytes) -> None:
    """Only a valid ID3v2 header is skipped."""
    assert_that(skip_id3v2(tag + b"\x00" * 16, 0)).is_none()


def test_id3_tag_filling_the_file_is_a_hint(tmp_path: Path) -> None:
    """A tag that ends at the end of the file has no audio after it."""
    target = tmp_path / "tag.bin"
    target.write_bytes(id3v2(32))

    result = detect(target)

    assert_that(result.media_type).is_none()


def test_too_many_id3_tags_stop_the_search(tmp_path: Path) -> None:
    """Only a bounded number of ID3v2 tags in a row are skipped."""
    target = tmp_path / "tags.bin"
    target.write_bytes(id3v2(4) * 6 + mpeg_frames())

    assert_that(detect(target).media_type).is_none()


# --- containers ------------------------------------------------------------------


def test_asf_header_shorter_than_its_fixed_part() -> None:
    """An ASF GUID without the rest of the header lists no stream."""
    assert_that(asf_streams(ASF_HEADER + b"\x00" * 4)).is_empty()


def test_asf_object_smaller_than_its_header_stops_the_walk() -> None:
    """An object size below 24 bytes ends the walk."""
    header = ASF_HEADER + struct.pack("<Q", 200) + b"\x01\x00\x00\x00\x01\x02"
    assert_that(asf_streams(header + b"\x00" * 16 + struct.pack("<Q", 8))).is_empty()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"\x00" * 20, [None]),
        (b"\x00" * 30 + b"\x04name", [None]),
        (b"\x00" * 30 + b"\x00\x0aaudio/x-ra", [AUDIO]),
    ],
    ids=["no-name", "no-mime", "empty-name"],
)
def test_realmedia_media_properties(body: bytes, expected: list[str | None]) -> None:
    """Short ``MDPR`` chunks are unrecognised streams."""
    data = b".RMF" + struct.pack(">IH", 18, 0) + b"\x00" * 8
    data += b"MDPR" + struct.pack(">IH", 10 + len(body), 0) + body

    assert_that(realmedia_streams(data)).is_equal_to(expected)


def test_realmedia_without_data_chunk_ends_with_the_bytes() -> None:
    """The walk stops at the end of the bytes read."""
    data = b".RMF" + struct.pack(">IH", 18, 0) + b"\x00" * 8 + b"PROP"

    assert_that(realmedia_streams(data)).is_empty()


def test_program_stream_mpeg1_pack(tmp_path: Path) -> None:
    """An MPEG-1 pack header (``0010`` marker) is 12 bytes long."""
    pack = b"\x00\x00\x01\xba\x21\x00\x01\x00\x01\x80\x00\x01"
    pes = b"\x00\x00\x01\xe0" + struct.pack(">H", 4) + b"\x0f\x00\x00\x00"
    target = tmp_path / "x.mpg"
    target.write_bytes(pack + pes + b"\x00" * 8)

    assert_that(detect(target).media_type).is_equal_to(MediaType.VIDEO)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (program_stream()[:14] + b"\x00\x00\x01\xba\xff" + b"\x00" * 9, []),
        (program_stream()[:14] + b"\x00\x00\x01\xba\x44\x00", []),
        (program_stream((0xC0, b"\x00")) + b"\xff" * 8, [AUDIO]),
        (program_stream((0xBD, b"")), [None]),
        (program_stream()[:14] + b"\x00\x00\x01\xbd\x00\x03\x81\x80", [None]),
        (program_stream((0xBD, b"\x55")), [None]),
        (program_stream()[:14] + b"\x00\x00\x01\xbd\x00\x03\x81\x80\x20", [None]),
        (program_stream((0xF0, b"")), [None]),
    ],
    ids=[
        "bad-second-pack",
        "cut-second-pack",
        "junk-after",
        "private-empty",
        "private-cut",
        "private-unknown",
        "private-header-past-end",
        "other-id",
    ],
)
def test_program_stream_packets(data: bytes, expected: list[str | None]) -> None:
    """Malformed or unrecognised packets give no or unknown streams."""
    assert_that(program_stream_streams(data)).is_equal_to(expected)


def test_ogg_cut_segment_table() -> None:
    """A BOS page whose segment table is cut lists no stream."""
    page = ogg_page(VORBIS_ID)

    assert_that(ogg_streams(page[:27])).is_empty()


def test_ogg_packet_spanning_full_segments() -> None:
    """A first packet of whole 255-byte segments is read across them."""
    packet = VORBIS_ID + b"\x00" * (510 - len(VORBIS_ID))
    page = ogg_page(packet)
    table_end = 27 + page[26]
    trimmed = page[:26] + bytes([2]) + b"\xff\xff" + page[table_end:]

    assert_that(ogg_streams(trimmed)).is_equal_to([AUDIO])


# --- EBML and transport streams ------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"\x1a\x45\xdf\xa3\x00",
        b"\x1a\x45\xdf\xa3\x40",
        b"\x1a\x45\xdf\xa3",
    ],
    ids=["zero-size-byte", "cut-size", "no-size"],
)
def test_invalid_ebml_size(tmp_path: Path, data: bytes) -> None:
    """An EBML header without a readable size is malformed."""
    target = tmp_path / "x.webm"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("matroska")


def _packet(
    pid: int, payload: bytes, *, unit_start: bool = True, control: int = 1
) -> bytes:
    """Build a 188-byte transport packet.

    Args:
        pid: Packet identifier.
        payload: Payload, padded with ``FF``.
        unit_start: Payload unit start indicator.
        control: Adaptation field control.

    Returns:
        The packet.
    """
    header = bytes(
        [0x47, (0x40 if unit_start else 0) | (pid >> 8), pid & 0xFF, control << 4]
    )
    return header + payload + b"\xff" * (184 - len(payload))


_PAT_WITH_NIT = (
    b"\x00\x00\xb0\x11\x00\x01\xc1\x00\x00"
    + b"\x00\x00\xe0\x10"  # program 0: the NIT
    + b"\x00\x01\xf0\x00"  # program 1: PMT PID 0x1000
    + b"\x00" * 4
)
_PMT_AUDIO = (
    b"\x00\x02\xb0\x12\x00\x01\xc1\x00\x00\xe1\x00\xf0\x00\x0f\xe1\x01\xf0\x00"
    + b"\x00" * 4
)


def test_ts_tables_with_nit_and_adaptation_field(tmp_path: Path) -> None:
    """The NIT entry is skipped and adaptation fields are stepped over."""
    adaptation = b"\x07\x10" + b"\x00" * 6
    packets = [
        _packet(0, _PAT_WITH_NIT),
        _packet(0x1000, adaptation + _PMT_AUDIO, control=3),
        _packet(0x1000, b"", control=2),
        *[_packet(0x1FFF, b"") for _ in range(4)],
    ]
    target = tmp_path / "x.ts"
    target.write_bytes(b"".join(packets))

    assert_that(detect(target).media_type).is_equal_to(MediaType.AUDIO)


@pytest.mark.parametrize(
    "pmt",
    [
        b"\x00\x02\xb0\x05\x00\x01\x00\x00\x00",  # shorter than its fixed part
        b"\x00\x05\xb0\x09\x00\x01\xc1\x00\x00\xe1\x00\x00\x00\x00\x00",  # not a PMT
    ],
    ids=["short-pmt", "other-table"],
)
def test_ts_without_a_usable_pmt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pmt: bytes,
) -> None:
    """A PMT PID without a readable PMT leaves the tables incomplete."""
    monkeypatch.setattr("winnow.media._stream_probe.find_binary", lambda _: None)
    packets = [
        _packet(0, _PAT_WITH_NIT),
        _packet(0x1000, pmt),
        *[_packet(0x1FFF, b"") for _ in range(4)],
    ]
    target = tmp_path / "x.ts"
    target.write_bytes(b"".join(packets))

    result = detect(target)

    assert_that(result.media_type).is_none()


def test_ts_section_fragments_without_a_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation with no section started, or a cut section, is ignored."""
    monkeypatch.setattr("winnow.media._stream_probe.find_binary", lambda _: None)
    adaptation = bytes([181]) + b"\x00" * 181
    packets = [
        _packet(0, b"\x00" * 8, unit_start=False),
        _packet(0, adaptation + b"\x00\x00", control=3),
        *[_packet(0x1FFF, b"") for _ in range(4)],
    ]
    target = tmp_path / "x.ts"
    target.write_bytes(b"".join(packets))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("mpeg-ts")


def test_ts_with_a_broken_packet_before_the_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Table reading stops at the first packet without a sync byte."""
    monkeypatch.setattr("winnow.media._stream_probe.find_binary", lambda _: None)
    packets = [_packet(0x1FFF, b"") for _ in range(5)]
    packets.append(b"\x00" * 188)
    packets.append(_packet(0, _PAT_WITH_NIT))
    target = tmp_path / "x.ts"
    target.write_bytes(b"".join(packets))

    assert_that(detect(target).media_type).is_none()


def test_reader_read_exact_reports_a_cut_structure() -> None:
    """A structure the file ends inside is malformed."""
    reader = Reader(handle=io.BytesIO(b"abc"), size=3, budget=64)

    with pytest.raises(MalformedError, match="truncated thing"):
        reader.read_exact(0, 8, "thing")


# --- signature checks ----------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"\x00\x00\x01\x00\x00\x00\x10\x10\x00\x00\x01\x00" + b"\x00" * 10,
        b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x05\x01\x00" + b"\x00" * 10,
        b"<!-- never closed <svg/>",
        b"<!DOCTYPE svg [ <!ENTITY a 'b'> <svg/>",
        b"<?xml version='1.0'",
        bytes([0x0A, 5, 0, 8]) + b"\x00" * 124,
    ],
    ids=[
        "ico-no-images",
        "ico-reserved",
        "svg-open-comment",
        "svg-open-doctype",
        "svg-open-pi",
        "pcx-encoding-0",
    ],
)
def test_near_miss_signatures(tmp_path: Path, data: bytes) -> None:
    """A near miss of an image signature is not an image."""
    target = tmp_path / "x.bin"
    target.write_bytes(data)

    assert_that(detect(target).media_type).is_none()
