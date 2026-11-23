"""Regression tests for the second independent review of PR 454 (#393).

Each test failed on 75ab42f. ffprobe is absent unless a test records an
answer or needs the real one.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

import puremagic
import pytest
from assertpy import assert_that

from tests.media.detect_support import FFPROBE, detect_with_deadline
from tests.media.signature_support import ogg_page, program_stream
from winnow.media import _stream_probe
from winnow.media._detect import DetectionResult, detect
from winnow.media._ffprobe import BoundedRun
from winnow.media.registry import FormatRegistry
from winnow.models.media import MediaType

_AUDIO_ONLY = b'{"streams": [{"index": 0, "codec_type": "audio"}]}'

_REAL_FFPROBE_TESTS = frozenset({"test_hostile_tables_are_fast_with_ffprobe"})
"""Tests that run with the real ffprobe, when it is installed."""


@pytest.fixture(autouse=True)
def _no_ffprobe(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Make ffprobe absent unless the test needs the real one.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        request: The requesting test.
    """
    if request.node.originalname not in _REAL_FFPROBE_TESTS:
        monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)


def _with_ffprobe(monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> list[str]:
    """Record an ffprobe answer and the demuxers it was asked with.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        stdout: Recorded JSON.

    Returns:
        Demuxers asked, in order.
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


def _detect_bytes(
    tmp_path: Path,
    data: bytes,
    name: str = "x.bin",
    *,
    registry: FormatRegistry | None = None,
) -> DetectionResult:
    """Write ``data`` and detect it.

    Args:
        tmp_path: Directory to write in.
        data: File bytes.
        name: File name.
        registry: Registry to detect with.

    Returns:
        The result.
    """
    target = tmp_path / name
    target.write_bytes(data)
    return detect(target, registry=registry)


def _decision(result: DetectionResult) -> str:
    """Return the deciding detail.

    Args:
        result: Detection result.

    Returns:
        The ``decision=`` detail.
    """
    return next(s.detail for s in result.signals if s.detail.startswith("decision="))


# --- blocker 1: text-based images and their extensions -----------------------

_XBM_BITS = b"static unsigned char img_bits[] = { 0x00 };\n"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        (
            "x.xbm",
            b"/* Created with GIMP */\n#define img_width 8\n#define img_height 1\n"
            + _XBM_BITS,
        ),
        ("x.xbm", b"\n\n#define img_width 8\n#define img_height 1\n" + _XBM_BITS),
        (
            "x.svg",
            b'<svg:svg xmlns:svg="http://www.w3.org/2000/svg" width="1" height="1"/>\n',
        ),
        (
            "x.xpm",
            b'\n/* XPM */\nstatic char *x[] = {"1 1 1 1", "a c #000000", "a"};\n',
        ),
        ("x.dxf", b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n"),
        ("x.eps", b"%!PS-Adobe-3.0\n%%BoundingBox: 0 0 1 1\nshowpage\n"),
        ("x.ai", b"%!PS-Adobe-3.0\n%%Creator: Adobe Illustrator\n%%EOF\n"),
    ],
    ids=[
        "xbm-gimp-comment",
        "xbm-blank-lines",
        "svg-prefixed",
        "xpm-blank-line",
        "dxf",
        "eps",
        "ai",
    ],
)
def test_text_media_under_its_own_extension(
    tmp_path: Path, name: str, data: bytes
) -> None:
    """A text-based image keeps its own extension's type."""
    result = _detect_bytes(tmp_path, data, name)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)


@pytest.mark.parametrize("name", ["notes.mp3", "notes.jpg", "notes.mp4"])
def test_text_under_a_binary_media_extension_is_still_not_media(
    tmp_path: Path, name: str
) -> None:
    """The text rule still wins over binary media extensions."""
    result = _detect_bytes(tmp_path, b"shopping list: eggs, milk\n", name)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("text")


@pytest.mark.parametrize(
    "data",
    [
        b"/* Created with GIMP */\n#define img_width 8\n#define img_height 1\n"
        + _XBM_BITS,
        b"#define my-image_width 8\n#define my-image_height 1\n"
        b"static char my-image_bits[] = {0};\n",
        b"#define width 8\n#define height 1\nstatic char bits[] = {0x00};\n",
        b"#define a_height 1\n#define a_width 8\nstatic char a_bits[] = {0x00};\n",
        b"#define a_width 16\n#define a_height 1\nstatic short a_bits[] = {0x0000};\n",
        b"#define a_width 8\n#define a_height 1\n#define a_x_hot 0\n"
        b"static char a_bits[] = {0};\n",
        b'<svg:svg xmlns:svg="http://www.w3.org/2000/svg"/>',
        b"<x:svg xmlns:x='http://www.w3.org/2000/svg'>\n</x:svg>",
        b'\n  /* XPM */\nstatic char *x[] = {"1 1 1 1"};\n',
    ],
    ids=[
        "xbm-comment",
        "xbm-dash-name",
        "xbm-no-prefix",
        "xbm-height-first",
        "xbm-x10-short",
        "xbm-hot-spot",
        "svg-prefix",
        "svg-prefix-single-quotes",
        "xpm-leading-space",
    ],
)
def test_loosened_text_signatures_when_renamed(tmp_path: Path, data: bytes) -> None:
    """These text images are typed from content alone."""
    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(MediaType.IMAGE)


@pytest.mark.parametrize(
    "data",
    [
        b"#define a_width 8\n#define b_height 1\nstatic char a_bits[] = {0};\n",
        b"#define a_width 8\n#define a_height 1\nint a_bits[] = {0};\n",
        b"#define a_width 0\n#define a_height 1\nstatic char a_bits[] = {0};\n",
        b'<x:svg xmlns:x="http://example.invalid/not-svg"/>',
        b"<x:svg/>",
    ],
    ids=[
        "xbm-names-differ",
        "xbm-int-array",
        "xbm-zero-width",
        "svg-other-namespace",
        "svg-unbound",
    ],
)
def test_text_near_misses_stay_text(tmp_path: Path, data: bytes) -> None:
    """Mismatched XBM names, other arrays and unbound prefixes are not images."""
    assert_that(_detect_bytes(tmp_path, data).media_type).is_none()


# --- blocker 2: printable magic in plain text ---------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"x" * 1080 + b"M.K." + b" more text\n",
        b"y" * 1080 + b"8CHN" + b" more text\n",
        b"NSVs are great\n",
        b"NSVf format notes\n",
        b"#!AMR\nnot audio, a note\n",
        b"MAC & PC notes\n",
        b"IMPM is a tracker magic\n",
        b"MMD001, a catalogue number\n",
        b"SMK2, a part number\n",
        b"BIKi is a word\n",
        b"XMF_1.00 is a version string\n",
        bytes.fromhex("49494e31") + b" is a code\n",
        b"MP+G and other notes\n",
        b"PSID registry entry\n",
    ],
    ids=[
        "mod-mk",
        "mod-8chn",
        "nsvs",
        "nsvf",
        "amr",
        "mac",
        "impm",
        "mmd0",
        "smk2",
        "biki",
        "xmf",
        "niff",
        "musepack",
        "psid",
    ],
)
def test_plain_text_with_a_printable_magic_is_not_media(
    tmp_path: Path, data: bytes
) -> None:
    """A binary format's printable magic does not make text media."""
    result = _detect_bytes(tmp_path, data)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("text")


def test_mod_requires_sample_headers_in_range(tmp_path: Path) -> None:
    """A MOD tag over sample headers past OpenMPT's 40 points is not MOD."""
    data = bytearray(1084 + 1024)
    data[950] = 1
    data[1080:1084] = b"M.K."
    for sample in range(21):
        data[20 + sample * 30 + 24] = 0x40
        data[20 + sample * 30 + 25] = 0x41
    assert_that(_detect_bytes(tmp_path, bytes(data)).media_type).is_none()


def test_amr_requires_a_valid_frame_type(tmp_path: Path) -> None:
    """An AMR magic followed by a frame header with padding bits set is not AMR."""
    assert_that(
        _detect_bytes(tmp_path, b"#!AMR\n\x83\x00\x01\x02").media_type
    ).is_none()


# --- blocker 3: hostile transport stream tables ------------------------------


def _hostile_ts() -> bytes:
    """Build 347 PAT packets naming 42 programs with section numbers 0 to 255.

    Returns:
        The stream.
    """
    packets = []
    for index in range(347):
        programs = b"".join(
            struct.pack(">HH", n + 1, 0xE000 | (0x100 + n)) for n in range(42)
        )
        body = struct.pack(">HBBB", 1, 0xC1, index % 256, 255) + programs + b"\x00" * 4
        section = bytes([0x00, 0xB0 | (len(body) >> 8), len(body) & 0xFF]) + body
        payload = b"\x00" + section
        packet = bytes([0x47, 0x40, 0x00, 0x10]) + payload
        packets.append(packet[:188].ljust(188, b"\xff"))
    return b"".join(packets)


def test_hostile_tables_do_not_ask_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tables past the caps are unknown without asking ffprobe."""
    asked = _with_ffprobe(monkeypatch, _AUDIO_ONLY)

    result = _detect_bytes(tmp_path, _hostile_ts(), "x.ts")

    assert_that(asked).is_empty()
    assert_that(result.media_type).is_none()
    assert_that(_decision(result)).contains("exceed their caps")


@pytest.mark.skipif(FFPROBE is None, reason="ffprobe not installed")
def test_hostile_tables_are_fast_with_ffprobe(tmp_path: Path) -> None:
    """With ffprobe present, the hostile stream still returns quickly."""
    target = tmp_path / "x.ts"
    target.write_bytes(_hostile_ts())

    start = time.perf_counter()
    detect_with_deadline(target)

    assert_that(time.perf_counter() - start).is_less_than(3.0)


def test_detection_probe_time_out_is_two_seconds() -> None:
    """The detection probe gives up after about two seconds."""
    assert_that(_stream_probe.PROBE_TIMEOUT_SECONDS).is_less_than_or_equal_to(2.0)


# --- blocker 4: formats main types that were neither signed nor listed --------


@pytest.mark.parametrize(
    "name",
    ["video-cd", "adif", "dec-au", "wave64", "qoi", "jng", "modi", "openraster"],
)
def test_new_signatures_when_renamed(tmp_path: Path, name: str) -> None:
    """Video CD, ADIF, DEC AU, Wave64, QOI, JNG, MODI and OpenRaster are typed."""
    from tests.media.library_support import LIBRARY_SAMPLES

    _, _, data, _, expected = next(s for s in LIBRARY_SAMPLES if s[0] == name)

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(expected)


# --- blocker 5: audio from a window of a stream ------------------------------


def _flv_tag(tag_type: int, body_bytes: int) -> bytes:
    """Build one FLV tag with its PreviousTagSize.

    Args:
        tag_type: 8 audio, 9 video.
        body_bytes: Data length.

    Returns:
        The tag.
    """
    tag = (
        bytes([tag_type])
        + body_bytes.to_bytes(3, "big")
        + b"\x00" * 7
        + b"\x00" * body_bytes
    )
    return tag + struct.pack(">I", len(tag))


def test_flv_video_after_many_audio_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Video starting at tag 1,726 is found by the native walk, not ffprobe."""
    asked = _with_ffprobe(monkeypatch, _AUDIO_ONLY)
    tags = [_flv_tag(8, 200)] * 1725 + [_flv_tag(9, 500)] + [_flv_tag(8, 200)] * 200
    data = b"FLV\x01\x04" + struct.pack(">I", 9) + b"\x00" * 4 + b"".join(tags)

    result = _detect_bytes(tmp_path, data, "clip.flv")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(asked).is_empty()


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (program_stream((0xC0, b"\x00" * 8), (0xC0, b"\x00" * 8)), "x.mpg"),
    ],
    ids=["mpeg-ps-no-system-header"],
)
def test_windowed_audio_from_ffprobe_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    name: str,
) -> None:
    """ffprobe's audio-only answer after an incomplete walk is not taken."""
    _with_ffprobe(monkeypatch, _AUDIO_ONLY)

    result = _detect_bytes(tmp_path, data, name)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result)).contains("ffprobe read only a window")


def test_declared_containers_keep_ffprobe_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where streams are declared up front (Ogg), ffprobe's audio stands."""
    _with_ffprobe(monkeypatch, _AUDIO_ONLY)
    data = ogg_page(b"\x01vorbis" + b"\x00" * 23) + ogg_page(
        b"\x80kate\x00\x00\x00" + b"\x00" * 56, serial=2
    )
    data += ogg_page(b"\x00" * 8, bos=False)

    assert_that(_detect_bytes(tmp_path, data, "x.ogg").media_type).is_equal_to(
        MediaType.AUDIO
    )


# --- smaller findings ---------------------------------------------------------


def _ts_packet(pid: int, section: bytes) -> bytes:
    """Build a transport packet starting a PSI section.

    Args:
        pid: Packet identifier.
        section: The section.

    Returns:
        The packet.
    """
    data = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10, 0x00]) + section
    return data + b"\xff" * (188 - len(data))


def _psi(table_id: int, extension: int, body: bytes, *, version: int = 0) -> bytes:
    """Build a current PSI section with a dummy CRC.

    Args:
        table_id: Table ID.
        extension: ``transport_stream_id`` or ``program_number``.
        body: Bytes after ``last_section_number``.
        version: ``version_number``.

    Returns:
        The section.
    """
    rest = (
        struct.pack(">HBBB", extension, 0xC1 | (version << 1), 0, 0)
        + body
        + b"\x00" * 4
    )
    return bytes([table_id, 0xB0 | (len(rest) >> 8), len(rest) & 0xFF]) + rest


def test_newer_pmt_after_complete_tables_is_read(tmp_path: Path) -> None:
    """The walk reads the whole head, so a later PMT version is seen."""
    pat = _psi(0x00, 1, struct.pack(">HH", 1, 0xE100))
    audio = _psi(0x02, 1, b"\xe1\x01\xf0\x00\x0f\xe1\x01\xf0\x00", version=0)
    video = _psi(0x02, 1, b"\xe1\x01\xf0\x00\x1b\xe1\x02\xf0\x00", version=1)
    nulls = [b"\x47\x1f\xff\x10" + b"\xff" * 184] * 4
    packets = [
        _ts_packet(0, pat),
        _ts_packet(0x100, audio),
        *nulls,
        _ts_packet(0x100, video),
    ]

    assert_that(
        _detect_bytes(tmp_path, b"".join(packets), "x.ts").media_type
    ).is_equal_to(
        MediaType.VIDEO,
    )


def test_open_comment_marker_is_not_a_closed_comment(tmp_path: Path) -> None:
    """``<!-->`` does not close the comment it opens."""
    assert_that(_detect_bytes(tmp_path, b"<!--><svg/>").media_type).is_none()


def test_playlist_extension_beats_an_agreeing_puremagic_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playlist extension is non-media even when puremagic agrees with it."""
    match = puremagic.main.PureMagicWithConfidence(
        byte_match=b"\x01\x02\x03",
        offset=0,
        extension=".m3u",
        mime_type="audio/x-mpegurl",
        name="playlist",
        confidence=0.9,
    )
    monkeypatch.setattr(puremagic, "magic_stream", lambda _: [match])

    result = _detect_bytes(tmp_path, b"\x01\x02\x03 binary\x00", "list.m3u")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("playlist")


@pytest.mark.parametrize(
    "doctype",
    [
        b'<!DOCTYPE svg SYSTEM "odd>name.dtd">',
        b'<!DOCTYPE svg [ <!ENTITY e "a]b"> ]>',
        b"<!DOCTYPE svg [ <!-- it's here --> ]>",
    ],
    ids=["gt-in-literal", "bracket-in-entity", "quote-in-comment"],
)
def test_svg_doctype_with_quoted_literals(tmp_path: Path, doctype: bytes) -> None:
    """Quoted ``>`` or ``]`` in a DOCTYPE does not end it early."""
    data = (
        b'<?xml version="1.0"?>\n'
        + doctype
        + b'\n<svg xmlns="http://www.w3.org/2000/svg"/>'
    )

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(MediaType.IMAGE)


# --- edges of the changes above ----------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"#!AMR_MC1.0\n\x00\x00\x00\x01\x3c" + b"\x00" * 31, MediaType.AUDIO),
        (b"#!AMR_MC1.0\n\x00\x00\x01\x01\x3c" + b"\x00" * 31, None),
        (b"#!AMR_MC1.0\n\x00\x00\x00\x00\x3c" + b"\x00" * 31, None),
        (b"#!AMR_MC1.0\n\x00\x00", None),
    ],
    ids=["multichannel", "reserved-bits", "no-channels", "cut-description"],
)
def test_amr_multichannel_description(
    tmp_path: Path, data: bytes, expected: MediaType | None
) -> None:
    """The multichannel forms need a valid channel description."""
    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(expected)


def test_mod_order_table_holds_pattern_numbers(tmp_path: Path) -> None:
    """An order entry of 128 or more is not a pattern number."""
    from tests.media.library_support import LIBRARY_SAMPLES

    data = bytearray(next(s for s in LIBRARY_SAMPLES if s[0] == "mod")[2])
    data[960] = 200

    assert_that(_detect_bytes(tmp_path, bytes(data)).media_type).is_none()


@pytest.mark.parametrize(
    "data",
    [
        b"<!DOCTYPE svg [ <!-- never closed",
        b'<!DOCTYPE svg "never closed',
        b"#define a_width 1\n/* never closed",
    ],
    ids=["doctype-comment", "doctype-literal", "xbm-comment"],
)
def test_unclosed_markup_is_not_an_image(tmp_path: Path, data: bytes) -> None:
    """An unclosed comment or literal in the prolog is not an image."""
    assert_that(_detect_bytes(tmp_path, data).media_type).is_none()


def test_flv_tag_headers_without_a_descriptor() -> None:
    """Without a descriptor the walk reads headers through the shared reader."""
    import io

    from winnow.media._detect_base import Content, Reader
    from winnow.media._packet_streams import flv_streams

    tags = [_flv_tag(8, 16)] * 3
    data = b"FLV\x01\x04" + struct.pack(">I", 9) + b"\x00" * 4 + b"".join(tags)

    def content(budget: int) -> Content:
        reader = Reader(handle=io.BytesIO(data), size=len(data), budget=budget)
        return Content(
            head=data[:20],
            tail=data[-32:],
            size=len(data),
            reader=reader,
            descriptor=-1,
        )

    assert_that(flv_streams(content(256)).complete).is_true()
    assert_that(flv_streams(content(0)).complete).is_false()
