"""Regression tests for the second review of pull request 454 (#393).

Each test failed on 66eb9d5. ffprobe is stubbed: absent unless a test
records an answer, so only the bytes decide.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.ebml_support import AUDIO as MKV_AUDIO
from tests.media.ebml_support import (
    SEGMENT,
    cluster,
    ebml_header,
    element,
    matroska,
    size,
    tracks,
)
from tests.media.ebml_support import VIDEO as MKV_VIDEO
from tests.media.signature_support import (
    KATE_ID,
    VORBIS_ID,
    asf,
    avi,
    id3v2,
    mpeg_frames,
    ogg_page,
    program_stream,
    realmedia,
    riff,
    riff_chunk,
)
from tests.media.test_detect_walks import _AUDIO, _VIDEO, _asf, _extension, _properties
from winnow.media import _containers, _stream_probe
from winnow.media._detect import DetectionResult, detect
from winnow.media._ffprobe import BoundedRun
from winnow.media._signatures import HEAD_BYTES
from winnow.models.media import MediaType

_AUDIO_ONLY = b'{"streams": [{"index": 0, "codec_type": "audio"}]}'


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ffprobe absent unless a test records an answer.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)


def _with_ffprobe(monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> list[str]:
    """Record an ffprobe answer and the demuxers it was asked with.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        stdout: Recorded JSON.

    Returns:
        The list each ``-format_whitelist`` value is appended to.
    """
    asked: list[str] = []

    def _run(argv: list[str], **_: object) -> BoundedRun:
        asked.append(argv[argv.index("-format_whitelist") + 1])
        return BoundedRun(
            returncode=0, stdout=stdout, stderr=b"", timed_out=False, overflowed=False
        )

    monkeypatch.setattr(
        _stream_probe, "find_binary", lambda _: "/opt/ffmpeg/bin/ffprobe"
    )
    monkeypatch.setattr(_stream_probe, "fd_protocol_supported", lambda _: True)
    monkeypatch.setattr(_stream_probe, "run_bounded", _run)
    return asked


def _detect_bytes(tmp_path: Path, data: bytes, name: str = "x.bin") -> DetectionResult:
    """Write ``data`` and detect it.

    Args:
        tmp_path: Directory to write in.
        data: File bytes.
        name: File name.

    Returns:
        The result.
    """
    target = tmp_path / name
    target.write_bytes(data)
    return detect(target)


def _details(result: DetectionResult) -> list[str]:
    """Return every signal detail.

    Args:
        result: Detection result.

    Returns:
        The details.
    """
    return [signal.detail for signal in result.signals]


def _decision(result: DetectionResult) -> str:
    """Return the deciding detail.

    Args:
        result: Detection result.

    Returns:
        The ``decision=`` detail.
    """
    return next(d for d in _details(result) if d.startswith("decision="))


# --- blocker 1: unbounded recursion --------------------------------------------


def _deep_avi(levels: int) -> bytes:
    """Build an AVI with ``levels`` nested ``LIST strl`` chunks.

    Args:
        levels: Nesting depth.

    Returns:
        The file bytes.
    """
    inner = riff_chunk(b"strh", b"auds" + b"\x00" * 52)
    for _ in range(levels):
        inner = riff_chunk(b"LIST", b"strl" + inner)
    return riff(b"RIFF", b"AVI ", riff_chunk(b"LIST", b"hdrl" + inner))


def _deep_asf(levels: int) -> bytes:
    """Build an ASF with ``levels`` nested Header Extension Objects.

    Args:
        levels: Nesting depth.

    Returns:
        The file bytes.
    """
    inner = _properties(_AUDIO, 1)
    for _ in range(levels):
        inner = _extension(inner)
    return _asf(inner)


@pytest.mark.parametrize(
    "data",
    [_deep_avi(1100), _deep_asf(1100)],
    ids=["avi-1100-strl", "asf-1100-extensions"],
)
def test_deep_nesting_does_not_raise(tmp_path: Path, data: bytes) -> None:
    """Deep nesting ends the walk as incomplete, never as RecursionError."""
    result = _detect_bytes(tmp_path, data)

    assert_that(result.media_type).is_none()
    assert_that(_details(result)).contains("streams=incomplete")


@pytest.mark.parametrize("levels", [2, 6])
def test_depth_cap_makes_the_list_incomplete(tmp_path: Path, levels: int) -> None:
    """Nesting past the depth cap is incomplete; within it the list stands."""
    result = _detect_bytes(tmp_path, _deep_avi(levels))

    expected = MediaType.AUDIO if levels < _containers.MAX_DEPTH else None
    assert_that(result.media_type).is_equal_to(expected)


# --- blocker 2: ID3v2 tags read once --------------------------------------------


@pytest.mark.parametrize("tags", [1, 3, 20])
@pytest.mark.parametrize("name", ["song.bin", "song", "song.jpg", "song.mp3"])
def test_tagged_mp3_with_several_tags(tmp_path: Path, tags: int, name: str) -> None:
    """An 81 KiB MP3 behind several ID3v2 tags is audio under any name."""
    data = id3v2(0) * tags + mpeg_frames(200)

    result = _detect_bytes(tmp_path, data, name)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_decision(result)).contains("after id3v2")


def test_tagged_mp3_with_a_large_tag(tmp_path: Path) -> None:
    """A tag larger than the head is skipped by its header alone."""
    data = id3v2(150_000) + mpeg_frames(20)

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(MediaType.AUDIO)


# --- blocker 3: FLV is complete only at the end of the file -------------------


def _flv_tag(tag_type: int, body_bytes: int) -> bytes:
    """Build one FLV tag with its PreviousTagSize.

    Args:
        tag_type: 8 audio, 9 video, 18 script.
        body_bytes: Tag data length.

    Returns:
        The tag.
    """
    tag = bytes([tag_type]) + body_bytes.to_bytes(3, "big") + b"\x00" * 7
    tag += b"\x00" * body_bytes
    return tag + struct.pack(">I", len(tag))


def _flv(flags: int, *tags: bytes) -> bytes:
    """Build an FLV file.

    Args:
        flags: Header flags (1 video, 4 audio).
        tags: Tags, each with its PreviousTagSize.

    Returns:
        The file bytes.
    """
    return (
        b"FLV\x01"
        + bytes([flags])
        + struct.pack(">I", 9)
        + b"\x00" * 4
        + b"".join(tags)
    )


def test_flv_audio_flag_with_late_video_tags_is_video(tmp_path: Path) -> None:
    """Flags 0x04 and a 66 KiB script tag before H.264 tags is video."""
    data = _flv(0x04, _flv_tag(18, 66 * 1024), _flv_tag(9, 32), _flv_tag(8, 16))

    result = _detect_bytes(tmp_path, data, "clip.flv")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_flv_without_flags_and_late_audio_reaches_the_end(tmp_path: Path) -> None:
    """Flags 0x00 and the first media tag past 64 KiB: the walk reads on."""
    data = _flv(0x00, _flv_tag(18, 66 * 1024), *[_flv_tag(8, 32)] * 4)

    result = _detect_bytes(tmp_path, data, "song.flv")

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_details(result)).contains("streams=complete")


def test_flv_cut_before_its_end_asks_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An FLV whose tags do not reach the end exactly is incomplete.

    ffprobe reads only a window, so its audio-only answer is not taken.
    """
    asked = _with_ffprobe(monkeypatch, _AUDIO_ONLY)
    data = _flv(0x04, *[_flv_tag(8, 32)] * 4)[:-7]

    result = _detect_bytes(tmp_path, data, "song.flv")

    assert_that(asked).is_equal_to(["flv"])
    assert_that(result.media_type).is_none()
    assert_that(_details(result)).contains("streams=incomplete")
    assert_that(_decision(result)).contains("ffprobe read only a window")


# --- blocker 4: an unrecognised stream asks ffprobe ---------------------------

_KATE_OGG = (
    ogg_page(VORBIS_ID) + ogg_page(KATE_ID, serial=2) + ogg_page(b"\x00" * 8, bos=False)
)


@pytest.mark.parametrize(
    ("data", "demuxer"),
    [
        (_KATE_OGG, "ogg"),
        (avi(b"auds", b"zzzz"), "avi"),
        (asf("audio", "other"), "asf"),
        (realmedia(b"audio/x-pn-realaudio", b"application/x-other"), "rm"),
    ],
    ids=["ogg-kate", "avi", "asf", "realmedia"],
)
def test_complete_list_with_an_unrecognised_stream_asks_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    demuxer: str,
) -> None:
    """A complete list with an unrecognised stream is settled by ffprobe."""
    asked = _with_ffprobe(monkeypatch, _AUDIO_ONLY)

    result = _detect_bytes(tmp_path, data)

    assert_that(asked).is_equal_to([demuxer])
    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)


def test_unrecognised_stream_without_ffprobe_says_so(tmp_path: Path) -> None:
    """Without ffprobe the signal names the unrecognised stream."""
    result = _detect_bytes(tmp_path, _KATE_OGG)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result)).contains("unrecognised stream, ffprobe needed")


# --- blocker 5: MPEG-PS keeps what it found -------------------------------------


def _pes(stream_id: int, payload_bytes: int) -> bytes:
    """Build one PES packet with an MPEG-2 header.

    Args:
        stream_id: Stream ID.
        payload_bytes: Payload length.

    Returns:
        The packet.
    """
    body = b"\x81\x80\x05\x21\x00\x01\x00\x01" + b"\x00" * payload_bytes
    return b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", len(body)) + body


_PACK2 = b"\x00\x00\x01\xba\x44\x00\x04\x00\x04\x01\x01\x89\xc3\xf8"
_PACK1 = b"\x00\x00\x01\xba\x21\x00\x01\x00\x01\x80\x00\x01"


@pytest.mark.parametrize("pack", [_PACK2, _PACK1], ids=["mpeg2", "mpeg1"])
def test_program_stream_pack_at_the_head_end_keeps_video(
    tmp_path: Path, pack: bytes
) -> None:
    """A pack header cut by the head's end does not discard the video seen."""
    video = _pes(0xE0, 64)
    padding = _pes(0xBE, HEAD_BYTES - len(_PACK2) - len(video) - 10 - 14)
    data = _PACK2 + video + padding + pack + _pes(0xC0, 64)
    assert_that(len(_PACK2 + video + padding)).is_equal_to(HEAD_BYTES - 10)

    result = _detect_bytes(tmp_path, data, "clip.mpg")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_mpeg1_pack_header_is_twelve_bytes(tmp_path: Path) -> None:
    """An MPEG-1 pack header ending the bytes read still counts as whole."""
    data = _PACK1 + b"\x00\x00\x01\xbb\x00\x06\x80\x00\x01\x04\x20\xff" + _pes(0xC0, 8)
    data += _PACK1

    result = _detect_bytes(tmp_path, data, "x.mpg")

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)


@pytest.mark.parametrize("tail", [b"\x12\x34\x56\x78", b""], ids=["corrupt", "cap"])
def test_program_stream_without_video_bound_needs_a_whole_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tail: bytes,
) -> None:
    """``video_bound`` 0 then corrupt bytes or the cap is not complete."""
    if not tail:
        monkeypatch.setattr(_containers, "MAX_CHUNKS", 3)
    packets = [(0xC0, b"\x00" * 8)] * 4
    data = program_stream(*packets, video_bound=0)[:-4] + tail

    result = _detect_bytes(tmp_path, data, "x.mpg")

    assert_that(result.media_type).is_none()
    assert_that(_details(result)).contains("streams=incomplete")


# --- blocker 6: MPEG-TS tables ---------------------------------------------------


def _section(
    table_id: int,
    body: bytes,
    *,
    extension: int = 1,
    number: int = 0,
    last: int = 0,
    current: bool = True,
) -> bytes:
    """Build a PSI section with a dummy CRC.

    Args:
        table_id: Table ID.
        body: Bytes after ``last_section_number``.
        extension: ``transport_stream_id`` or ``program_number``.
        number: ``section_number``.
        last: ``last_section_number``.
        current: ``current_next_indicator``.

    Returns:
        The section.
    """
    version = 0xC1 if current else 0xC0
    rest = extension.to_bytes(2, "big") + bytes([version, number, last]) + body
    rest += b"\x00" * 4
    return bytes([table_id, 0xB0 | (len(rest) >> 8), len(rest) & 0xFF]) + rest


def _packet(
    pid: int,
    payload: bytes,
    *,
    unit_start: bool = True,
    error: bool = False,
    scrambled: bool = False,
) -> bytes:
    """Build a 188-byte transport packet.

    Args:
        pid: Packet identifier.
        payload: Payload, pointer field included when a section starts.
        unit_start: Payload unit start indicator.
        error: Transport error indicator.
        scrambled: Set ``transport_scrambling_control`` to 2.

    Returns:
        The packet.
    """
    first = (0x80 if error else 0) | (0x40 if unit_start else 0) | (pid >> 8)
    control = 0x90 if scrambled else 0x10
    data = bytes([0x47, first, pid & 0xFF, control]) + payload
    return data + b"\xff" * (188 - len(data))


def _pat(*programs: tuple[int, int], current: bool = True) -> bytes:
    """Build a PAT section.

    Args:
        programs: ``(program_number, PMT PID)`` pairs.
        current: ``current_next_indicator``.

    Returns:
        The section.
    """
    body = b"".join(
        struct.pack(">HH", number, 0xE000 | pid) for number, pid in programs
    )
    return _section(0x00, body, current=current)


def _pmt(program: int, stream_type: int) -> bytes:
    """Build a PMT section with one stream.

    Args:
        program: ``program_number``.
        stream_type: The stream's ``stream_type``.

    Returns:
        The section.
    """
    return _section(
        0x02,
        b"\xe1\x00\xf0\x00" + bytes([stream_type]) + b"\xe1\x01\xf0\x00",
        extension=program,
    )


_NULLS = [_packet(0x1FFF, b"", unit_start=False)] * 5


@pytest.mark.parametrize("bad", ["error", "scrambled", "not-current"])
def test_ts_skips_bad_table_packets(tmp_path: Path, bad: str) -> None:
    """Errored, scrambled or not-yet-current tables are not read."""
    options = {"error": bad == "error", "scrambled": bad == "scrambled"}
    pat = _pat((1, 0x100), current=bad != "not-current")
    packets = [
        _packet(0, b"\x00" + pat, **options),
        _packet(0x100, b"\x00" + _pmt(1, 0x0F)),
    ]

    result = _detect_bytes(tmp_path, b"".join(packets + _NULLS), "x.ts")

    assert_that(result.media_type).is_none()
    assert_that(_decision(result)).contains("program tables incomplete")


def test_ts_programs_sharing_a_pmt_pid(tmp_path: Path) -> None:
    """A second program on the same PMT PID is read before deciding."""
    packets = [
        _packet(0, b"\x00" + _pat((1, 0x100), (2, 0x100))),
        _packet(0x100, b"\x00" + _pmt(1, 0x0F)),
        _packet(0x100, b"\x00" + _pmt(2, 0x1B)),
    ]

    result = _detect_bytes(tmp_path, b"".join(packets + _NULLS), "x.ts")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_ts_section_tail_before_the_pointer_field(tmp_path: Path) -> None:
    """The bytes before ``pointer_field`` finish the previous section."""
    language = b"\x0a\x04eng\x00" * 30
    loop = b"\x1b\xe1\x01\xf0" + bytes([len(language)]) + language
    pmt = _section(0x02, b"\xe1\x00\xf0\x00" + loop, extension=1)
    assert_that(len(pmt)).is_greater_than(183)
    first = _packet(0x100, b"\x00" + pmt[:183])
    tail = pmt[183:]
    second = _packet(0x100, bytes([len(tail)]) + tail + _pmt(1, 0x0F))
    packets = [_packet(0, b"\x00" + _pat((1, 0x100))), first, second]

    result = _detect_bytes(tmp_path, b"".join(packets + _NULLS), "x.ts")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_ts_two_sections_in_one_packet(tmp_path: Path) -> None:
    """A second section after the first in the same packet is read."""
    pat = _pat((1, 0x100), (2, 0x100))
    both = _pmt(1, 0x0F) + _pmt(2, 0x1B)
    packets = [_packet(0, b"\x00" + pat), _packet(0x100, b"\x00" + both)]

    result = _detect_bytes(tmp_path, b"".join(packets + _NULLS), "x.ts")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_ts_partial_tables_showing_video_are_video(tmp_path: Path) -> None:
    """A video stream in the PMTs read decides before every PMT is read."""
    packets = [
        _packet(0, b"\x00" + _pat((1, 0x100), (2, 0x200))),
        _packet(0x100, b"\x00" + _pmt(1, 0x1B)),
    ]

    result = _detect_bytes(tmp_path, b"".join(packets + _NULLS), "x.ts")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_ts_fallback_names_an_unrecognised_stream(tmp_path: Path) -> None:
    """Complete tables with an unrecognised stream type say so."""
    packets = [
        _packet(0, b"\x00" + _pat((1, 0x100))),
        _packet(0x100, b"\x00" + _pmt(1, 0x86)),
    ]

    result = _detect_bytes(tmp_path, b"".join(packets + _NULLS), "x.ts")

    assert_that(_decision(result)).contains("unrecognised stream type, ffprobe needed")


# --- blocker 7: ASF duplicate stream number --------------------------------------


@pytest.mark.parametrize(
    "objects",
    [
        (_properties(_VIDEO, 1), _properties(_AUDIO, 1)),
        (_properties(_AUDIO, 1), _properties(_VIDEO, 1)),
    ],
    ids=["video-then-audio", "audio-then-video"],
)
def test_asf_duplicate_stream_never_downgrades(
    tmp_path: Path, objects: tuple[bytes, ...]
) -> None:
    """A stream typed twice keeps video and makes the list incomplete."""
    result = _detect_bytes(tmp_path, _asf(*objects), "x.wmv")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_details(result)).contains("streams=incomplete")


def test_asf_duplicate_audio_is_incomplete(tmp_path: Path) -> None:
    """Two audio Stream Properties for one stream number are not proof."""
    result = _detect_bytes(
        tmp_path, _asf(_properties(_AUDIO, 1), _properties(_AUDIO, 1))
    )

    assert_that(result.media_type).is_none()


# --- late video, per container ---------------------------------------------------


def test_video_after_the_chunk_cap_is_never_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Video declared past the chunk cap leaves every walk incomplete."""
    monkeypatch.setattr(_containers, "MAX_CHUNKS", 6)
    files = {
        "x.avi": avi(*[b"auds"] * 6, b"vids"),
        "x.wmv": asf(*["audio"] * 6, "video"),
        "x.ogg": b"".join(ogg_page(VORBIS_ID, serial=i) for i in range(7))
        + ogg_page(b"\x80theora" + b"\x00" * 35, serial=9)
        + ogg_page(b"\x00" * 8, bos=False),
        "x.mpg": program_stream(
            *[(0xC0, b"\x00" * 8)] * 7, (0xE0, b"\x00" * 8), video_bound=0
        ),
        "x.flv": _flv(0x04, *[_flv_tag(8, 8)] * 7, _flv_tag(9, 8)),
        "x.rm": realmedia(*[b"audio/x-pn-realaudio"] * 6, b"video/x-pn-realvideo"),
    }
    for name, data in files.items():
        result = _detect_bytes(tmp_path, data, name)
        assert_that(result.media_type).described_as(name).is_not_equal_to(
            MediaType.AUDIO
        )


def test_video_in_a_second_program_is_never_audio(tmp_path: Path) -> None:
    """A second program whose PMT is not read yet keeps TS from audio."""
    packets = [
        _packet(0, b"\x00" + _pat((1, 0x100), (2, 0x200))),
        _packet(0x100, b"\x00" + _pmt(1, 0x0F)),
    ]

    result = _detect_bytes(tmp_path, b"".join(packets + _NULLS), "x.ts")

    assert_that(result.media_type).is_none()


# --- smaller findings ------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"#define NAME_width 80\n#define OTHER 1\nint main(void) { return 0; }\n",
        b"#define a_width 8\n#define a_height 1\n/* no bits array */\n",
        b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x00\x01\x00\x20\x00"
        + struct.pack("<II", 0, 22)
        + b"\x00" * 40,
        b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x00\x01\x00\x20\x00"
        + struct.pack("<II", 40, 0)
        + b"\x00" * 40,
        b"\x01\xda\x01\x01" + struct.pack(">4H", 3, 0, 0, 3) + b"\x00" * 500,
        b"\x01\xda\x01\x01" + struct.pack(">4H", 3, 1, 1, 3) + b"\x00" * 20,
    ],
    ids=[
        "c-header",
        "xbm-no-bits",
        "ico-empty",
        "ico-offset-0",
        "sgi-0x0",
        "sgi-short",
    ],
)
def test_near_miss_image_headers(tmp_path: Path, data: bytes) -> None:
    """Headers that fail the full structure are not images."""
    result = _detect_bytes(tmp_path, data)

    assert_that(result.media_type).is_none()


@pytest.mark.parametrize(
    "hdrl",
    [
        riff_chunk(b"strh", b"")
        + riff_chunk(b"LIST", b"strl" + riff_chunk(b"strh", b"vids")),
        riff_chunk(b"LIST", b"strl" + riff_chunk(b"strh", b"auds"))
        + b"strh\xff\x00\x00\x00",
        riff_chunk(b"LIST", b"strl" + riff_chunk(b"strh", b"auds")) + b"str",
    ],
    ids=["zero-length-strh", "overflowing-child", "cut-child"],
)
def test_avi_bad_hdrl_child_is_incomplete(tmp_path: Path, hdrl: bytes) -> None:
    """A short ``strh`` or a cut or overflowing child is not a whole list."""
    data = riff(b"RIFF", b"AVI ", riff_chunk(b"LIST", b"hdrl" + hdrl))

    result = _detect_bytes(tmp_path, data, "x.avi")

    assert_that(result.media_type).is_not_equal_to(MediaType.AUDIO)
    assert_that(_details(result)).contains("streams=incomplete")


def test_dvd_substream_needs_an_mpeg2_pes_header(tmp_path: Path) -> None:
    """``private_stream_1`` with an MPEG-1 style header is not read as DVD."""
    # MPEG-1 header byte 0F, then payload 80 00 80: read as MPEG-2, the
    # third byte would be a header length of 0 and point at an AC-3 ID.
    mpeg1_pes = b"\x00\x00\x01\xbd\x00\x04\x0f\x80\x00\x80"
    data = program_stream(video_bound=0)[:-4] + mpeg1_pes + b"\x00\x00\x01\xb9"

    result = _detect_bytes(tmp_path, data, "x.vob")

    assert_that(result.media_type).is_none()
    assert_that(_details(result)).contains("stream=unrecognised")


def test_ogg_bos_cut_by_the_head_keeps_streams(tmp_path: Path) -> None:
    """A later BOS page cut by the head keeps the streams found, incomplete."""
    data = ogg_page(b"\x80theora" + b"\x00" * 35)
    serial = 2
    while HEAD_BYTES - 28 - len(data) > 150:
        data += ogg_page(VORBIS_ID + b"\x00" * 42, serial=serial)
        serial += 1
    data += ogg_page(
        VORBIS_ID + b"\x00" * (HEAD_BYTES - 28 - len(data) - 28 - 30), serial=serial
    )
    assert_that(len(data)).is_equal_to(HEAD_BYTES - 28)
    data += ogg_page(VORBIS_ID + b"\x00" * 300, serial=serial + 1)

    result = _detect_bytes(tmp_path, data, "x.ogv")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_bw64_with_another_form_is_unknown(tmp_path: Path) -> None:
    """``BW64`` with a form type other than WAVE is recognised and unknown."""
    result = _detect_bytes(tmp_path, riff(b"BW64", b"XXXX", b"data"), "x.wav")

    assert_that(result.container).is_equal_to("riff")
    assert_that(result.media_type).is_none()


def test_matroska_tracks_past_the_segment_is_malformed(tmp_path: Path) -> None:
    """``Tracks`` reaching past the Segment's end is not read."""
    body = tracks(MKV_AUDIO)
    data = ebml_header() + SEGMENT + size(len(body) - 3) + body + cluster()

    result = _detect_bytes(tmp_path, data, "x.mka")

    assert_that(result.media_type).is_none()
    assert_that(_details(result)).contains(
        "tracks=unresolved malformed: master element extends past the Segment",
    )


def test_matroska_second_tracks_is_read(tmp_path: Path) -> None:
    """A second ``Tracks`` before the first Cluster is not ignored."""
    data = matroska(tracks(MKV_AUDIO), tracks(MKV_VIDEO), cluster())

    result = _detect_bytes(tmp_path, data, "x.mka")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_details(result)).contains("tracks=audio,video")


def test_matroska_gates_on_read_version_only(tmp_path: Path) -> None:
    """``EBMLVersion`` 2 with ``EBMLReadVersion`` 1 is readable."""
    header = element(
        bytes.fromhex("1a45dfa3"),
        b"\x42\x86\x81\x02\x42\xf7\x81\x01" + element(bytes.fromhex("4282"), b"webm"),
    )
    body = tracks(MKV_AUDIO) + cluster()
    data = header + SEGMENT + size(len(body)) + body

    result = _detect_bytes(tmp_path, data, "x.webm")

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
