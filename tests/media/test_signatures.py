"""Tests for rule 4 of media-type detection: the signature table (#393).

Header-only cases are tiny literal headers named ``.bin`` (so no extension
helps) and assert detection only. Extraction uses committed fixtures.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import puremagic
import pytest
from assertpy import assert_that

from tests.media.detect_support import (
    AUDIO_FIXTURES,
    FIXTURES,
    decision,
    details,
    needs_ffprobe,
    record_dispatch,
)
from tests.media.signature_support import (
    KATE_ID,
    OPUS_HEAD,
    SKELETON,
    THEORA_ID,
    VORBIS_ID,
    ac3_frames,
    adts_frames,
    asf,
    avi,
    dts14_frames,
    dts_frames,
    dts_le_frames,
    eac3_frames,
    flv,
    id3v2,
    mpeg_frames,
    ogg_page,
    program_stream,
    realmedia,
    riff,
)
from winnow.media import _detect, _signatures, _stream_probe
from winnow.media._detect import detect
from winnow.media._header_checks import TEXT_BYTES, is_text
from winnow.media._signatures import SIGNATURES
from winnow.media.service import DefaultMetadataService
from winnow.models.media import MediaType

IMAGE = MediaType.IMAGE
AUDIO = MediaType.AUDIO
VIDEO = MediaType.VIDEO

_PCX = bytes([0x0A, 5, 1, 8]) + struct.pack("<4H", 0, 0, 31, 23) + b"\x00" * 116
_SGI = b"\x01\xda\x01\x01" + struct.pack(">4H", 3, 1, 1, 3) + b"\x00" * 500
_TGA = (
    b"\x00\x00\x02"
    + b"\x00" * 15
    + b"\x00" * 64
    + b"\x00" * 8
    + b"TRUEVISION-XFILE.\x00"
)
_SVG = b'<?xml version="1.0"?>\n<!-- c -->\n<!DOCTYPE svg [ <!ENTITY a "b"> ]>\n<svg/>'

_ICO_ENTRY = (
    b"\x00\x00\x02\x00\x01\x00"
    + b"\x10\x10\x00\x00\x01\x00\x20\x00"
    + struct.pack("<II", 40, 22)
    + b"\x00" * 40
)
_FITS = (b"SIMPLE  =" + b" " * 20 + b"T").ljust(80) + b"END".ljust(80)

_HEADERS: list[tuple[str, bytes, str, MediaType]] = [
    ("jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "jpeg", IMAGE),
    ("png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "png", IMAGE),
    ("gif87a", b"GIF87a\x01\x00\x01\x00", "gif", IMAGE),
    ("gif89a", b"GIF89a\x01\x00\x01\x00", "gif", IMAGE),
    ("bmp", b"BM" + b"\x00" * 12 + struct.pack("<I", 40) + b"\x00" * 36, "bmp", IMAGE),
    ("tiff-le", b"II*\x00\x08\x00\x00\x00", "tiff", IMAGE),
    ("tiff-be", b"MM\x00*\x00\x00\x00\x08", "tiff", IMAGE),
    ("bigtiff-le", b"II+\x00\x08\x00\x00\x00\x10\x00", "tiff", IMAGE),
    ("bigtiff-be", b"MM\x00+\x00\x08\x00\x00\x00\x00", "tiff", IMAGE),
    ("orf", b"IIRO\x08\x00\x00\x00", "olympus-orf", IMAGE),
    ("orf-be", b"MMOR\x00\x00\x00\x08", "olympus-orf", IMAGE),
    ("rw2", b"IIU\x00\x18\x00\x00\x00", "panasonic-rw2", IMAGE),
    ("raf", b"FUJIFILMCCD-RAW 0201FF383501", "fujifilm-raf", IMAGE),
    ("x3f", bytes.fromhex("464f5662") + b"\x00" * 4, "sigma-x3f", IMAGE),
    ("mrw", b"\x00MRM\x00\x00\x00\x00", "minolta-mrw", IMAGE),
    ("ari", b"ARRI\x12\x34\x56\x78", "arri-ari", IMAGE),
    ("webp", riff(b"RIFF", b"WEBP", b"VP8 "), "webp", IMAGE),
    (
        "ico",
        _ICO_ENTRY.replace(b"\x00\x00\x02\x00", b"\x00\x00\x01\x00", 1),
        "ico",
        IMAGE,
    ),
    (
        "cur",
        _ICO_ENTRY,
        "ico",
        IMAGE,
    ),
    ("jp2", b"\x00\x00\x00\x0cjP  \r\n\x87\n", "jpeg2000", IMAGE),
    ("j2k", b"\xff\x4f\xff\x51\x00\x2f", "jpeg2000", IMAGE),
    ("ppm", b"P6\n2 1\n255\n" + b"\x00" * 6, "netpbm", IMAGE),
    ("pbm-ascii", b"P1\n# c\n2 1\n0 1\n", "netpbm", IMAGE),
    ("pam", b"P7\nWIDTH 1\nHEIGHT 1\nDEPTH 3\nMAXVAL 255\nENDHDR\n", "netpbm", IMAGE),
    ("pcx", _PCX, "pcx", IMAGE),
    ("sgi", _SGI, "sgi", IMAGE),
    ("dds", b"DDS " + struct.pack("<I", 124) + b"\x00" * 120, "dds", IMAGE),
    ("icns", b"icns" + struct.pack(">I", 8), "icns", IMAGE),
    ("exr", b"\x76\x2f\x31\x01\x02\x00\x00\x00", "openexr", IMAGE),
    ("sun", b"\x59\xa6\x6a\x95" + b"\x00" * 28, "sun-raster", IMAGE),
    (
        "xbm",
        b"#define img_width 8\n#define img_height 1\n"
        b"static char img_bits[] = {0x00};\n",
        "xbm",
        IMAGE,
    ),
    ("xpm", b'/* XPM */\nstatic char *x[] = {"1 1 1 1"};\n', "xpm", IMAGE),
    ("xpm2", b"! XPM2\n1 1 1 1\na c #000000\na\n", "xpm", IMAGE),
    ("eps", b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\n", "eps", IMAGE),
    ("eps-dos", b"\xc5\xd0\xd3\xc6" + b"\x00" * 28, "eps", IMAGE),
    ("xcf", b"gimp xcf v011\x00" + b"\x00" * 16, "xcf", IMAGE),
    ("jpeg-xr", b"II\xbc\x01" + b"\x00" * 16, "jpeg-xr", IMAGE),
    ("ilbm", b"FORM\x00\x00\x00\x04ILBM", "ilbm", IMAGE),
    ("fits", _FITS, "fits", IMAGE),
    ("djvu", b"AT&TFORM\x00\x00\x00\x10DJVUINFO", "djvu", IMAGE),
    ("svg", _SVG, "svg", IMAGE),
    ("tga", _TGA, "tga", IMAGE),
    ("wav", riff(b"RIFF", b"WAVE", b"fmt "), "wav", AUDIO),
    ("rf64", riff(b"RF64", b"WAVE", b"ds64"), "wav", AUDIO),
    ("bw64", riff(b"BW64", b"WAVE", b"ds64"), "wav", AUDIO),
    ("aiff", b"FORM\x00\x00\x00\x04AIFF", "aiff", AUDIO),
    ("aifc", b"FORM\x00\x00\x00\x04AIFC", "aiff", AUDIO),
    ("flac", b"fLaC\x00\x00\x00\x22" + b"\x00" * 34, "flac", AUDIO),
    ("au", b".snd" + struct.pack(">5I", 24, 0, 3, 8000, 1), "au", AUDIO),
    ("au-le", b"dns." + struct.pack("<5I", 24, 0, 3, 8000, 1), "au", AUDIO),
    ("monkeys-audio", b"MAC \x96\x0f" + b"\x00" * 26, "monkeys-audio", AUDIO),
    ("musepack-sv7", b"MP+\x07" + b"\x00" * 24, "musepack", AUDIO),
    ("musepack-sv8", b"MPCKSH" + b"\x00" * 24, "musepack", AUDIO),
    ("midi", b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60", "midi", AUDIO),
    ("rmid", riff(b"RIFF", b"RMID", b"data"), "midi", AUDIO),
    ("dv", b"\x1f\x07\x00\x3f" + b"\x00" * 76, "dv", VIDEO),
    ("caf", b"caff\x00\x01\x00\x00desc", "caf", AUDIO),
    (
        "wavpack",
        b"wvpk" + struct.pack("<IH", 32, 0x410) + b"\x00" * 22,
        "wavpack",
        AUDIO,
    ),
    ("tta", b"TTA1\x01\x00\x02\x00", "tta", AUDIO),
    (
        "voc",
        b"Creative Voice File\x1a"
        + struct.pack("<3H", 26, 0x10A, (~0x10A + 0x1234) & 0xFFFF),
        "voc",
        AUDIO,
    ),
    ("realaudio", b".ra\xfd\x00\x04", "realaudio", AUDIO),
    (
        "mpeg-video",
        b"\x00\x00\x01\xb3\x0b\x00\x90\x13\xff\xff\xe0\x18",
        "mpeg-video",
        VIDEO,
    ),
    ("y4m", b"YUV4MPEG2 W2 H2 F25:1\nFRAME\n", "yuv4mpeg", VIDEO),
    ("mpeg-audio", mpeg_frames(), "mpeg-audio", AUDIO),
    ("adts", adts_frames(), "adts", AUDIO),
    ("adts-crc", adts_frames(crc=True), "adts", AUDIO),
    ("ac3", ac3_frames(), "ac3", AUDIO),
    ("eac3", eac3_frames(), "ac3", AUDIO),
    ("dts", dts_frames(), "dts", AUDIO),
    ("dts-le", dts_le_frames(), "dts", AUDIO),
    ("dts14-be", dts14_frames(), "dts", AUDIO),
    ("dts14-le", dts14_frames(little=True), "dts", AUDIO),
]


@pytest.mark.parametrize(
    ("data", "container", "expected"),
    [
        pytest.param(data, container, expected, id=name)
        for name, data, container, expected in _HEADERS
    ],
)
def test_signature_table_entry(
    tmp_path: Path,
    data: bytes,
    container: str,
    expected: MediaType,
) -> None:
    """Each signature decides its type from content alone (header only)."""
    target = tmp_path / "sample.bin"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_equal_to(container)
    assert_that(decision(result).source).is_equal_to("signature")


def test_every_table_entry_is_tested() -> None:
    """Every container the table names has a header-only test row."""
    tested = {container for _, _, container, _ in _HEADERS}
    named = {s.container for s in SIGNATURES if not s.non_media and s.streams is None}

    assert_that(sorted(named - tested)).is_empty()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (avi(b"vids", b"auds"), VIDEO),
        (avi(b"auds"), AUDIO),
        (avi(b"auds", b"txts"), AUDIO),
        (avi(b"auds", b"zzzz"), None),
        (avi(), None),
        (asf("video", "audio"), VIDEO),
        (asf("audio"), AUDIO),
        (asf("audio", "command"), AUDIO),
        (asf("audio", "other"), None),
        (flv(0x05, 18, 8, 9), VIDEO),
        (flv(0x04, 18, 8, 8), AUDIO),
        (flv(0x04, 8, 9), VIDEO),
        (flv(0x00), None),
        (realmedia(b"video/x-pn-realvideo", b"audio/x-pn-realaudio"), VIDEO),
        (realmedia(b"audio/x-pn-realaudio", b"logical-fileinfo"), AUDIO),
        (realmedia(b"application/x-other"), None),
        (program_stream((0xE0, b"\x00" * 8), (0xC0, b"\x00" * 8)), VIDEO),
        (program_stream((0xC0, b"\x00" * 8), video_bound=0), AUDIO),
        (program_stream((0xC0, b"\x00" * 8)), None),
        (program_stream((0xC0, b"\x00" * 8), video_bound=1), None),
        (
            program_stream(
                (0xBD, b"\x80\x01\x00\x01"), (0xBE, b"\xff" * 8), video_bound=0
            ),
            AUDIO,
        ),
        (program_stream((0xBD, b"\x20\x00")), None),
    ],
    ids=[
        "avi-video",
        "avi-audio",
        "avi-audio-text",
        "avi-unknown-stream",
        "avi-no-strh",
        "asf-video",
        "asf-audio",
        "asf-audio-command",
        "asf-unknown-stream",
        "flv-video",
        "flv-audio",
        "flv-video-tag",
        "flv-none",
        "rm-video",
        "rm-audio",
        "rm-unknown",
        "ps-video",
        "ps-audio",
        "ps-audio-no-system-header",
        "ps-audio-video-bound",
        "ps-dvd-ac3",
        "ps-subpicture-only",
    ],
)
def test_container_streams_decide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    expected: MediaType | None,
) -> None:
    """A container's stream declarations, not its magic, decide its type."""
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)
    target = tmp_path / "clip.mp4"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_in("avi", "asf", "flv", "realmedia", "mpeg-ps")
    assert_that(decision(result).source).is_equal_to("signature")


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ((VORBIS_ID, THEORA_ID), VIDEO),
        ((THEORA_ID, VORBIS_ID), VIDEO),
        ((OPUS_HEAD,), AUDIO),
        ((SKELETON, VORBIS_ID), AUDIO),
        ((VORBIS_ID, KATE_ID), None),
        ((SKELETON,), None),
    ],
    ids=[
        "vorbis-theora",
        "theora-vorbis",
        "opus",
        "skeleton-vorbis",
        "kate",
        "skeleton",
    ],
)
def test_ogg_bos_streams(
    tmp_path: Path,
    pages: tuple[bytes, ...],
    expected: MediaType | None,
) -> None:
    """Every beginning-of-stream page counts, not only the first."""
    data = b"".join(
        ogg_page(packet, serial=index) for index, packet in enumerate(pages)
    )
    data += ogg_page(b"\x00" * 16, bos=False)
    target = tmp_path / "clip.ogg"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_equal_to("ogg")


def test_ogg_vorbis_then_theora(tmp_path: Path) -> None:
    """A Vorbis-first, Theora-second Ogg file is video."""
    target = tmp_path / "movie.ogg"
    target.write_bytes(ogg_page(VORBIS_ID) + ogg_page(THEORA_ID, serial=2))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(VIDEO)
    assert_that(details(result)).contains("stream=audio", "stream=video")


def test_ogg_without_bos_first_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Ogg file whose first page is not a BOS page is unknown."""
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)
    target = tmp_path / "x.ogg"
    target.write_bytes(ogg_page(VORBIS_ID, bos=False))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(details(result)).contains("streams=incomplete")
    assert_that(decision(result).detail).contains("ffprobe needed")


# --- ID3v2 and frame-based audio ---------------------------------------------


def test_id3_without_mpeg_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ID3v2 tag alone is a hint, not proof of MP3."""
    monkeypatch.setattr(puremagic, "magic_stream", lambda _: [])
    target = tmp_path / "tagged.bin"
    target.write_bytes(id3v2(64) + b"\x00\x01\x02 not audio frames" * 8)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).is_equal_to("decision=extension fallback")
    assert_that(details(result)).contains(
        "hint=id3v2 tag without audio frames after it"
    )


@pytest.mark.parametrize(
    ("audio", "container"),
    [
        (mpeg_frames(), "mpeg-audio"),
        (adts_frames(), "adts"),
        (b"fLaC\x00\x00\x00\x22" + b"\x00" * 34, "flac"),
    ],
    ids=["mp3", "adts", "flac"],
)
@pytest.mark.parametrize(
    "tag",
    [id3v2(32), id3v2(70_000), id3v2(16, version=4, footer=True), id3v2(8) + id3v2(8)],
    ids=["small", "past-head", "v4-footer", "two-tags"],
)
def test_id3_then_audio(
    tmp_path: Path,
    audio: bytes,
    container: str,
    tag: bytes,
) -> None:
    """The synchsafe size is skipped and the audio after it decides."""
    target = tmp_path / "tagged.bin"
    target.write_bytes(tag + audio)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(AUDIO)
    assert_that(result.container).is_equal_to(container)
    assert_that(decision(result).detail).contains("after id3v2").is_not_empty()


def test_id3_past_the_read_budget_is_a_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio after a tag beyond the read budget is not looked for."""
    monkeypatch.setattr(_detect, "SIGNATURE_BYTES_READ", _signatures.HEAD_BYTES + 64)
    target = tmp_path / "tagged.bin"
    target.write_bytes(id3v2(70_000) + mpeg_frames())

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(details(result)).contains("hint=id3v2 tag past the read budget")


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xfb\x90\x00" + b"\x00" * 64,
        mpeg_frames(1) + b"\xff\xfb" + b"\x00" * 64,
        b"\xff\xfb\xf0\x00" * 400,
        b"\xff\xf1\x50\x80\x00\x1f\xfc" + b"\x00" * 64,
        b"\x0b\x77\x00\x00\xc0\x40" + b"\x00" * 600,
        b"\x7f\xfe\x80\x01\x00\x00\x00\x00" + b"\x00" * 64,
    ],
    ids=[
        "one-mp3-frame",
        "broken-chain",
        "bad-bitrate",
        "short-adts",
        "ac3-fscod-3",
        "dts",
    ],
)
def test_lone_sync_patterns_are_not_audio(tmp_path: Path, data: bytes) -> None:
    """A sync pattern without consecutive valid frames is not audio."""
    target = tmp_path / "noise.bin"
    target.write_bytes(data)

    assert_that(detect(target).media_type).is_none()


def test_frames_ending_exactly_at_the_file_end(tmp_path: Path) -> None:
    """Fewer frames than the minimum count when they end the file exactly."""
    target = tmp_path / "short.bin"
    target.write_bytes(mpeg_frames(2))

    assert_that(detect(target).media_type).is_equal_to(AUDIO)


def test_two_byte_ac3_sync_alone_is_not_audio(tmp_path: Path) -> None:
    """A text file opening with ``0b 77`` is not AC-3."""
    target = tmp_path / "notes.txt"
    target.write_bytes(b"\x0b\x77\x00\x00Ghello world")

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).is_equal_to("decision=extension fallback")


# --- RIFF and IFF with other form types --------------------------------------


@pytest.mark.parametrize(
    ("data", "container"),
    [
        (riff(b"RIFF", b"CDXA", b"data"), "riff"),
        (riff(b"BW64", b"XXXX", b"data"), "riff"),
        (b"FORM\x00\x00\x00\x048SVX", "iff"),
    ],
    ids=["riff-cdxa", "bw64-other", "form-8svx"],
)
def test_unlisted_form_type_is_unknown(
    tmp_path: Path, data: bytes, container: str
) -> None:
    """A RIFF or FORM container whose form type is not listed is unknown."""
    target = tmp_path / "x.wav"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to(container)
    assert_that(decision(result).detail).contains("unknown unsupported: form type")


# --- text --------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"P1 is the highest priority\n",
        b"P6 report\n",
        b"#define NOT_AN_IMAGE 1\n",
        b"<html><svg/></html>",
    ],
    ids=["p1-word", "p6-word", "define", "svg-not-root"],
)
def test_text_resembling_an_image_header_is_text(tmp_path: Path, data: bytes) -> None:
    """Text needs the full header structure to be an image."""
    target = tmp_path / "notes.txt"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("text")


def test_text_check_allows_a_character_cut_by_the_window() -> None:
    """A multi-byte character cut at the window edge is still text."""
    head = b"a" * (TEXT_BYTES - 1) + "é".encode() + b"tail"

    assert_that(is_text(head, size=len(head))).is_true()
    assert_that(is_text(b"\xff\xfe binary", size=9)).is_false()
    assert_that(is_text(b"", size=0)).is_false()


# --- formats main detected through puremagic ---------------------------------

_NON_BMFF_AUDIO = (
    ("ac3.ac3", "ac3"),
    ("ac3_mono.ac3", "ac3"),
    ("eac3.eac3", "ac3"),
    ("tta.tta", "tta"),
    ("wavpack.wv", "wavpack"),
    ("core_audio.bin", "caf"),
    ("au.au", "au"),
)


@pytest.mark.parametrize(("name", "container"), _NON_BMFF_AUDIO)
@pytest.mark.parametrize("rename", ["", ".bin", ".jpg"])
def test_non_bmff_audio_has_a_signature(
    tmp_path: Path,
    name: str,
    container: str,
    rename: str,
) -> None:
    """Audio ``main`` found through puremagic now has its own signature."""
    target = tmp_path / (name + rename)
    shutil.copyfile(AUDIO_FIXTURES / name, target)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(AUDIO)
    assert_that(result.container).is_equal_to(container)
    assert_that(decision(result).source).is_equal_to("signature")


@pytest.mark.parametrize(("name", "container"), _NON_BMFF_AUDIO)
def test_service_dispatches_non_bmff_audio(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    container: str,
) -> None:
    """The service sends audio outside ISO-BMFF to the audio reader."""
    del container
    dispatched = record_dispatch(monkeypatch)

    DefaultMetadataService().extract(AUDIO_FIXTURES / name)

    assert_that(dispatched).is_equal_to([AUDIO])


@needs_ffprobe
@pytest.mark.parametrize(
    "name", ["ac3.ac3", "ac3_mono.ac3", "eac3.eac3", "tta.tta", "wavpack.wv"]
)
def test_service_extracts_non_bmff_audio(name: str) -> None:
    """Audio outside ISO-BMFF gets its duration, as on ``main``."""
    metadata = DefaultMetadataService().extract(AUDIO_FIXTURES / name)

    assert_that(metadata.duration_seconds).is_not_none()
    assert_that(metadata.sample_rate).is_not_none()


@pytest.mark.parametrize(
    ("name", "container"),
    [("sample.jpg", "jpeg"), ("sample.mp3", "mpeg-audio"), ("sample.flac", "flac")],
)
def test_fixture_renamed_keeps_its_type(
    tmp_path: Path, name: str, container: str
) -> None:
    """Repository fixtures are typed by content under any name."""
    target = tmp_path / "renamed"
    shutil.copyfile(FIXTURES / name, target)

    result = detect(target)

    assert_that(result.container).is_equal_to(container)
    assert_that(result.media_type).is_not_none()
