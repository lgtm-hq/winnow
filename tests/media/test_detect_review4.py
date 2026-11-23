"""Regression tests for the third independent review of PR 454 (#393).

The extension-only and text-media marker tests are in
``test_detect_review4_markers.py``.

Each finding's tests failed on 3db4394; the mutation tests kill the
mutations the review found surviving. ffprobe is absent throughout.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.detect_support import FFMPEG, run_ffmpeg
from tests.media.signature_support import program_stream
from winnow.media import _stream_probe
from winnow.media._detect import DetectionResult, detect
from winnow.media._format_checks import AMR_FRAMES_CHECKED
from winnow.media._header_checks import is_text
from winnow.media._packet_streams import MAX_FLV_TAGS, MAX_PROGRAM_STREAM_PACKETS
from winnow.media._text_formats import TEXT_BASED_CONTAINERS
from winnow.media.registry import FormatRegistry
from winnow.models.media import MediaType

AUDIO = MediaType.AUDIO
IMAGE = MediaType.IMAGE
VIDEO = MediaType.VIDEO


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ffprobe absent.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)


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


def _details(result: DetectionResult) -> list[str]:
    """Return every signal detail.

    Args:
        result: Detection result.

    Returns:
        The details.
    """
    return [signal.detail for signal in result.signals]


_NAMES = ("clip", "clip.bin", "clip")
"""A wrong extension, a neutral one, and none, filled in per test."""


# --- blocker 1: printable YUV4MPEG2, AMR and the other text-capable formats --


def _y4m(header: bytes = b"YUV4MPEG2 W64 H32 F25:1 Ip A1:1 C420jpeg\n") -> bytes:
    """Build a YUV4MPEG2 stream of three dim grey frames, all printable.

    Args:
        header: The stream header line.

    Returns:
        The stream.
    """
    frame = b"FRAME\n" + b"(" * (64 * 32) + b"\x7e" * (64 * 32 // 2)
    return header + frame * 3


@pytest.mark.parametrize("name", ["clip.y4m", "clip.bin", "clip"])
def test_printable_yuv4mpeg_is_video(tmp_path: Path, name: str) -> None:
    """A YUV4MPEG2 file whose first 8 KiB are printable is video."""
    data = _y4m()
    assert_that(is_text(data, size=len(data))).is_true()

    assert_that(_detect_bytes(tmp_path, data, name).media_type).is_equal_to(VIDEO)


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_ffmpeg_gradient_yuv4mpeg_is_video(tmp_path: Path) -> None:
    """The reviewer's gradient clip is video under its name and renamed."""
    target = tmp_path / "grad.y4m"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "gradients=s=1280x720:c0=0x202830:c1=0x606870:d=0.2:r=25",
        "-pix_fmt",
        "yuv420p",
        str(target),
    )
    data = target.read_bytes()
    assert_that(is_text(data[:65536], size=len(data))).is_true()

    assert_that(detect(target).media_type).is_equal_to(VIDEO)
    renamed = target.rename(tmp_path / "grad.bin")
    assert_that(detect(renamed).media_type).is_equal_to(VIDEO)


@pytest.mark.parametrize(
    "header",
    [
        b"YUV4MPEG2 H32 F25:1\nFRAME\n",
        b"YUV4MPEG2 W64 F25:1\nFRAME\n",
        b"YUV4MPEG2 Wide H32\nFRAME\n",
        b"YUV4MPEG2 W64 H32\nFRAMEX\n",
        b"YUV4MPEG2 W64 H32 F25:1\n",
    ],
    ids=["no-width", "no-height", "width-not-digits", "no-frame", "header-only"],
)
def test_yuv4mpeg_needs_size_and_a_frame(tmp_path: Path, header: bytes) -> None:
    """Without ``W``, ``H`` and a ``FRAME`` after the header it is not video."""
    data = header + b"\x00\x01\x02" * 8

    assert_that(_detect_bytes(tmp_path, data).media_type).is_none()


def test_yuv4mpeg_prose_is_text(tmp_path: Path) -> None:
    """Prose that starts with the magic is plain text."""
    result = _detect_bytes(tmp_path, b"YUV4MPEG2 is a raw video format.\n")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("text")


def _amr_frames(count: int, header: int = 0x24, speech: bytes = b"A") -> bytes:
    """Build AMR-NB frames of one frame type (default FT 4, ``$``).

    Args:
        count: Frames.
        header: Frame header byte.
        speech: Byte repeated for the speech bits.

    Returns:
        The frames.
    """
    sizes = {0x24: 19, 0x2C: 20, 0x34: 26, 0x3C: 31, 0x44: 5}
    size = sizes.get(header, 0)
    return (bytes([header]) + speech * size) * count


@pytest.mark.parametrize("name", ["voice.amr", "voice.bin", "voice"])
def test_printable_amr_is_audio(tmp_path: Path, name: str) -> None:
    """AMR made only of printable frames (headers ``$``, speech ``A``) is audio."""
    data = b"#!AMR\n" + _amr_frames(500)
    assert_that(is_text(data, size=len(data))).is_true()

    assert_that(_detect_bytes(tmp_path, data, name).media_type).is_equal_to(AUDIO)


@pytest.mark.parametrize(
    "data",
    [
        b"#!AMR\n$ is a dollar sign; this note is about AMR files.\n" * 20,
        b"#!AMR\nD is SID and so on, but this is just a note.\n" * 20,
    ],
    ids=["dollar", "sid"],
)
def test_amr_prose_is_text(tmp_path: Path, data: bytes) -> None:
    """Prose whose first byte after the magic is a valid frame header is text."""
    result = _detect_bytes(tmp_path, data, "note.amr")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("text")


def test_amr_frames_must_follow_each_other(tmp_path: Path) -> None:
    """A valid first frame then an invalid header is not AMR."""
    data = b"#!AMR\n" + _amr_frames(3) + b"\x83" + b"\x00" * 64

    assert_that(_detect_bytes(tmp_path, data).media_type).is_none()


def test_amr_walk_stops_after_its_frames(tmp_path: Path) -> None:
    """Only the first frames are checked: garbage after them does not matter."""
    data = b"#!AMR\n" + _amr_frames(AMR_FRAMES_CHECKED, speech=b"\x00") + b"\x83" * 8

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(AUDIO)


def test_text_based_containers_are_the_audited_list() -> None:
    """Exactly these signatures are tried on plain text (the audit's list)."""
    assert_that(set(TEXT_BASED_CONTAINERS)).is_equal_to(
        {
            "svg",
            "xbm",
            "xpm",
            "netpbm",
            "eps",
            "fits",
            "yuv4mpeg",
            "amr",
            "xmf",
            "kodak-kdc",
        },
    )


def _xmf(length: int | None = None, filler: bytes = b"x") -> bytes:
    """Build an XMF 1.00 file of 100 bytes with a one-byte file length.

    Args:
        length: File length to write; the true length by default.
        filler: Byte repeated after the header.

    Returns:
        The file.
    """
    size = 100
    written = size if length is None else length
    return b"XMF_1.00" + bytes([written]) + filler * (size - 9)


def test_printable_xmf_is_audio(tmp_path: Path) -> None:
    """An XMF file whose bytes are all printable is audio."""
    data = _xmf()
    assert_that(is_text(data, size=len(data))).is_true()

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(AUDIO)


@pytest.mark.parametrize("filler", [b"x", b"\x00"], ids=["text", "binary"])
def test_xmf_file_length_must_match(tmp_path: Path, filler: bytes) -> None:
    """An XMF header whose file length is not the file size is not XMF."""
    data = _xmf(length=99, filler=filler)

    assert_that(_detect_bytes(tmp_path, data).media_type).is_none()


def test_xmf_two_point_oh_reads_its_length_after_the_ids(tmp_path: Path) -> None:
    """XMF 2.00 has the file type and revision IDs before the length."""
    data = b"XMF_2.00" + b"\x00\x00\x00\x02\x00\x00\x00\x01" + b"\x81\x00"
    data += b"\x00" * (128 - len(data))

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(AUDIO)


def test_xmf_unterminated_length_is_not_xmf(tmp_path: Path) -> None:
    """A file length of more than four bytes is not a length."""
    data = b"XMF_1.00" + b"\xff" * 4 + b"\x00" * 64

    assert_that(_detect_bytes(tmp_path, data).media_type).is_none()


def test_printable_kodak_kdc_is_an_image(tmp_path: Path) -> None:
    """A KDC whose first 8 KiB are printable is still an image."""
    data = b"k" * 242 + b"EASTMAN KODAK COMPANY" + b"k" * 9000

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(IMAGE)


# --- smaller findings: MPEG-PS past the head -----------------------------------


def _audio_program_stream(packs: int) -> bytes:
    """Build an audio-only program stream of 2,048-byte packs.

    Args:
        packs: Packs after the first.

    Returns:
        The stream, ``video_bound`` 0, without an end code.
    """
    data = program_stream((0xC0, b"\x00" * 2000), video_bound=0)[:-4]
    pack = program_stream((0xC0, b"\x00" * 2016))[:-4]
    return data + pack * packs


def test_audio_program_stream_past_the_head_is_audio(tmp_path: Path) -> None:
    """A 2 MB audio-only program stream is walked to its end."""
    data = _audio_program_stream(1000)

    result = _detect_bytes(tmp_path, data, "song.mpg")

    assert_that(result.media_type).is_equal_to(AUDIO)
    assert_that(_details(result)).contains("streams=complete")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_ffmpeg_audio_program_stream_is_audio(tmp_path: Path) -> None:
    """A 60-second MPEG-1 Layer II program stream from ffmpeg is audio."""
    target = tmp_path / "song.mpg"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=d=60",
        "-c:a",
        "mp2",
        "-b:a",
        "192k",
        "-f",
        "mpeg",
        str(target),
    )

    assert_that(detect(target).media_type).is_equal_to(AUDIO)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [(MAX_PROGRAM_STREAM_PACKETS, AUDIO), (MAX_PROGRAM_STREAM_PACKETS + 1, None)],
    ids=["at-the-cap", "past-the-cap"],
)
def test_program_stream_packet_cap(
    tmp_path: Path, headers: int, expected: MediaType | None
) -> None:
    """The walk reads exactly the capped number of headers; past it, unknown."""
    start = program_stream(video_bound=0)[:-4]
    empty = b"\x00\x00\x01\xc0\x00\x00"
    data = start + empty * (headers - 2)

    result = _detect_bytes(tmp_path, data, "song.mpg")

    assert_that(result.media_type).is_equal_to(expected)


def test_program_stream_dvd_substream_past_the_head(tmp_path: Path) -> None:
    """A DVD AC-3 substream past the head is read with its header."""
    data = _audio_program_stream(40)
    body = b"\x81\x80\x20" + b"\x21\x00\x01\x00\x01" + b"\xff" * 27
    body += b"\x80" + b"\x00" * 16
    data += b"\x00\x00\x01\xbd" + struct.pack(">H", len(body)) + body

    result = _detect_bytes(tmp_path, data, "film.vob")

    assert_that(result.media_type).is_equal_to(AUDIO)


# --- smaller findings: mutations that survived ---------------------------------


def test_text_starting_like_an_iff_form_is_text(tmp_path: Path) -> None:
    """Plain text stops after the table: ``FORMAL`` is not an IFF form."""
    result = _detect_bytes(tmp_path, b"FORMAL letter, dear sir\n")

    assert_that(result.container).is_equal_to("text")


def _flv(tags: int) -> bytes:
    """Build an audio-only FLV of empty audio tags.

    Args:
        tags: Tags.

    Returns:
        The file.
    """
    tag = b"\x08" + b"\x00" * 10
    tag += struct.pack(">I", len(tag))
    return b"FLV\x01\x04" + struct.pack(">I", 9) + b"\x00" * 4 + tag * tags


@pytest.mark.parametrize(
    ("tags", "expected"),
    [(MAX_FLV_TAGS, AUDIO), (MAX_FLV_TAGS + 1, None)],
    ids=["at-the-cap", "past-the-cap"],
)
def test_flv_tag_cap(tmp_path: Path, tags: int, expected: MediaType | None) -> None:
    """The FLV walk reads exactly :data:`MAX_FLV_TAGS` tag headers."""
    result = _detect_bytes(tmp_path, _flv(tags), "song.flv")

    assert_that(result.media_type).is_equal_to(expected)


@pytest.mark.parametrize(
    ("header_size", "file_size", "expected"),
    [(27, 64, None), (28, 64, VIDEO), (64, 64, VIDEO), (65, 64, None)],
    ids=["below-28", "28", "file-size", "past-the-file"],
)
def test_nsvf_header_size(
    tmp_path: Path, header_size: int, file_size: int, expected: MediaType | None
) -> None:
    """NSVf's header size is at least 28 and at most the file size."""
    data = b"NSVf" + struct.pack("<I", header_size)
    data += b"\x00" * (file_size - len(data))

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(expected)


@pytest.mark.parametrize(
    ("rate", "length", "expected"),
    [(0, 32, None), (1, 32, VIDEO), (1, 19, VIDEO), (1, 18, None)],
    ids=["rate-zero", "rate-one", "nineteen-bytes", "eighteen-bytes"],
)
def test_nsvs_frame_rate(
    tmp_path: Path, rate: int, length: int, expected: MediaType | None
) -> None:
    """NSVs needs 19 bytes and a non-zero frame-rate byte at 16."""
    data = bytearray(b"NSVsVP62MP3 \x40\x01\xf0\x00" + bytes([rate]) + b"\x00" * 15)

    assert_that(_detect_bytes(tmp_path, bytes(data[:length])).media_type).is_equal_to(
        expected
    )


@pytest.mark.parametrize(
    ("magic", "frame_type", "expected"),
    [
        (b"#!AMR\n", 8, AUDIO),
        (b"#!AMR\n", 9, None),
        (b"#!AMR\n", 14, None),
        (b"#!AMR\n", 15, AUDIO),
        (b"#!AMR-WB\n", 9, AUDIO),
        (b"#!AMR-WB\n", 10, None),
        (b"#!AMR-WB\n", 14, AUDIO),
    ],
)
def test_amr_frame_types(
    tmp_path: Path, magic: bytes, frame_type: int, expected: MediaType | None
) -> None:
    """Only the frame types RFC 4867 defines for the format are accepted."""
    data = magic + bytes([frame_type << 3 | 0x04]) + b"\x00" * 64

    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(expected)


def test_prefixed_root_must_be_named_svg(tmp_path: Path) -> None:
    """A prefixed root bound to the SVG namespace but not ``svg`` is not SVG."""
    data = b'<x:rect xmlns:x="http://www.w3.org/2000/svg"/>\n'

    result = _detect_bytes(tmp_path, data)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("text")


# --- smaller findings: MOD sample headers real trackers write ----------------


def _mod(volume: int = 64, finetune: int = 0, samples: int = 1) -> bytes:
    """Build a four-channel MOD whose first samples have these fields.

    Args:
        volume: Volume byte of each set sample.
        finetune: Finetune byte of each set sample.
        samples: Samples set.

    Returns:
        The module.
    """
    data = bytearray(1084 + 1024)
    for sample in range(samples):
        header = 20 + sample * 30
        data[header + 22 : header + 24] = struct.pack(">H", 16)
        data[header + 24] = finetune
        data[header + 25] = volume
    data[950] = 1
    data[1080:1084] = b"M.K."
    return bytes(data)


@pytest.mark.parametrize(
    "data",
    [_mod(volume=65), _mod(finetune=0xF3), _mod(volume=65, finetune=0x1F, samples=20)],
    ids=["volume-65", "finetune-high-nibble", "twenty-samples"],
)
def test_mod_with_fields_real_trackers_write(tmp_path: Path, data: bytes) -> None:
    """Volume 65 and finetune high nibbles are accepted, as OpenMPT does."""
    assert_that(_detect_bytes(tmp_path, data).media_type).is_equal_to(AUDIO)


def test_mod_past_openmpts_threshold_is_not_mod(tmp_path: Path) -> None:
    """Forty-one invalid points (OpenMPT's threshold is 40) is not a module."""
    data = bytearray(_mod(volume=65, finetune=0x1F, samples=20))
    data[20 + 20 * 30 + 25] = 65

    assert_that(_detect_bytes(tmp_path, bytes(data)).media_type).is_none()


def test_mod_loop_start_past_the_sample_counts(tmp_path: Path) -> None:
    """A loop start past twice the length scores a point, as in OpenMPT."""
    data = bytearray(_mod(volume=65, finetune=0x1F, samples=20))
    data[20 + 20 * 30 + 26 : 20 + 20 * 30 + 28] = struct.pack(">H", 1)

    assert_that(_detect_bytes(tmp_path, bytes(data)).media_type).is_none()
