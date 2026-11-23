"""Tests for Matroska, WebM and MPEG transport stream detection (#393).

Stream types come from the PMT or from ffprobe. Tests that need ffprobe's
answer use recorded output (ffprobe 9.0.2) through a stubbed runner, so they
pass without FFmpeg; tests on real files are skipped without it.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.detect_support import (
    AUDIO_48K_IN,
    AUDIO_FIXTURES,
    AUDIO_IN,
    VIDEO_IN,
    decision,
    details,
    needs_ffprobe,
    run_ffmpeg,
)
from winnow.media import _stream_probe, audio
from winnow.media._detect import detect
from winnow.media._ffprobe import BoundedRun
from winnow.media._mpegts import classify_stream
from winnow.media.service import DefaultMetadataService
from winnow.models.media import AudioFlag, MediaType

AUDIO_ONLY = (
    b'{ "programs": [ ], "stream_groups": [ ], "streams": [ { "index": 0,'
    b' "codec_type": "audio", "disposition": { "attached_pic": 0 } } ]}'
)
"""ffprobe 9.0.2 stream list of an Opus-only WebM."""

VIDEO_AND_AUDIO = (
    b'{ "programs": [ ], "stream_groups": [ ], "streams": [ { "index": 0,'
    b' "codec_type": "video", "disposition": { "attached_pic": 0 } }, { "index": 1,'
    b' "codec_type": "audio", "disposition": { "attached_pic": 0 } } ]}'
)
"""ffprobe 9.0.2 stream list of a VP9 and Opus WebM."""

AUDIO_WITH_COVER = (
    b'{ "programs": [ ], "stream_groups": [ ], "streams": [ { "index": 0,'
    b' "codec_type": "audio", "disposition": { "attached_pic": 0 } }, { "index": 1,'
    b' "codec_type": "video", "disposition": { "attached_pic": 1 } } ]}'
)
"""ffprobe 9.0.2 stream list of audio with attached cover art."""


def _ebml(doctype: bytes = b"webm", *, extra: bytes = b"") -> bytes:
    """Build an EBML header followed by an empty Segment.

    Args:
        doctype: DocType value.
        extra: Further header elements.

    Returns:
        The file bytes.
    """
    body = b"\x42\x86\x81\x01" + b"\x42\xf7\x81\x01" + b"\x42\xf2\x81\x04"
    body += b"\x42\xf3\x81\x08" + b"\x42\x82" + bytes([0x80 | len(doctype)]) + doctype
    body += extra
    return (
        b"\x1a\x45\xdf\xa3" + bytes([0x80 | len(body)]) + body + b"\x18\x53\x80\x67\x80"
    )


def _stub_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
    run: BoundedRun | None,
    *,
    fd_protocol: bool = True,
) -> None:
    """Replace ffprobe with a recorded answer.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        run: The recorded run, or ``None`` for no ffprobe on ``PATH``.
        fd_protocol: Whether ffprobe offers the ``fd`` protocol.
    """
    monkeypatch.setattr(
        _stream_probe,
        "find_binary",
        lambda _: None if run is None else "/opt/ffmpeg/bin/ffprobe",
    )
    monkeypatch.setattr(_stream_probe, "fd_protocol_supported", lambda _: fd_protocol)
    monkeypatch.setattr(_stream_probe, "run_bounded", lambda *_, **__: run)


def _ok(stdout: bytes) -> BoundedRun:
    """Build a clean recorded run.

    Args:
        stdout: Recorded JSON.

    Returns:
        The run.
    """
    return BoundedRun(
        returncode=0, stdout=stdout, stderr=b"", timed_out=False, overflowed=False
    )


# --- rule 3: Matroska and WebM ------------------------------------------------


@pytest.mark.parametrize("name", ["audio.webm", "audio.mka", "audio.bin"])
def test_webm_without_probe_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """Unresolved tracks without ffprobe are unknown; the extension never decides."""
    _stub_ffprobe(monkeypatch, None)
    target = tmp_path / name
    target.write_bytes(_ebml())

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("matroska")
    assert_that(details(result)).contains(
        "ebml doctype=webm",
        "unavailable=ffprobe not found on PATH",
    )
    assert_that(decision(result).detail).is_equal_to(
        "decision=matroska unknown unavailable: tracks unresolved and ffprobe needed",
    )


def test_ffprobe_without_fd_protocol_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ffprobe without the ``fd`` protocol cannot inspect the open file."""
    _stub_ffprobe(monkeypatch, _ok(AUDIO_ONLY), fd_protocol=False)
    target = tmp_path / "x.webm"
    target.write_bytes(_ebml())

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(details(result)).contains("unavailable=ffprobe lacks the fd protocol")


@pytest.mark.parametrize(
    ("stdout", "expected", "streams"),
    [
        (AUDIO_ONLY, MediaType.AUDIO, "audio"),
        (VIDEO_AND_AUDIO, MediaType.VIDEO, "video,audio"),
        (AUDIO_WITH_COVER, MediaType.AUDIO, "audio,cover"),
        (b'{"streams": []}', None, "(none)"),
        (b'{"streams": [{"codec_type": "subtitle"}]}', None, "subtitle"),
    ],
    ids=["audio", "video-audio", "audio-cover", "no-streams", "subtitle"],
)
def test_matroska_stream_types_decide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    expected: MediaType | None,
    streams: str,
) -> None:
    """ffprobe's stream types decide Matroska; cover art is not video."""
    _stub_ffprobe(monkeypatch, _ok(stdout))
    target = tmp_path / "x.webm"
    target.write_bytes(_ebml())

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(details(result)).contains(f"ffprobe streams={streams}")


@pytest.mark.parametrize(
    ("run", "detail"),
    [
        (BoundedRun(1, b"", b"Invalid data", False, False), "ffprobe=exit 1"),
        (
            BoundedRun(-9, b"", b"", True, False),
            "ffprobe=exceeded its time or output bound",
        ),
        (_ok(b"not json"), "ffprobe=unreadable output"),
        (_ok(b"[1, 2]"), "ffprobe=no stream list"),
        (_ok(b'{"streams": [7]}'), "ffprobe=malformed stream entry"),
    ],
    ids=["exit-1", "timeout", "not-json", "no-list", "bad-entry"],
)
def test_failed_probe_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run: BoundedRun,
    detail: str,
) -> None:
    """A probe that fails establishes nothing."""
    _stub_ffprobe(monkeypatch, run)
    target = tmp_path / "x.mkv"
    target.write_bytes(_ebml(b"matroska"))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(details(result)).contains(detail)


def test_probe_that_cannot_start_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ffprobe that cannot start leaves the check unavailable."""
    _stub_ffprobe(monkeypatch, _ok(AUDIO_ONLY))

    def _fail(*_: object, **__: object) -> BoundedRun:
        raise OSError("exec format error")

    monkeypatch.setattr(_stream_probe, "run_bounded", _fail)
    target = tmp_path / "x.webm"
    target.write_bytes(_ebml())

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains("unknown unavailable")


@pytest.mark.parametrize(
    ("data", "container", "reason"),
    [
        (_ebml(b"avro"), "ebml", "unknown unsupported: doctype"),
        (b"\x1a\x45\xdf\xa3\x88\x42\x86\x81\x01", "matroska", "past the end"),
        (b"\x1a\x45\xdf\xa3\x84\x42\x86\x81\x01", "matroska", "no DocType"),
        (b"\x1a\x45\xdf\xa3\xff", "matroska", "EBML header size"),
        (_ebml(extra=b"\x42\xf7\x81\x02"), "matroska", "EBML version 2"),
        (_ebml(extra=b"\x42\xf2\x81\x08"), "matroska", "EBMLMaxIDLength 8"),
        (_ebml(extra=b"\x42\xf3\x81\x09"), "matroska", "EBMLMaxSizeLength 9"),
        (b"\x1a\x45\xdf\xa3\x83\x42\x86\x85\x01", "matroska", "past the header"),
    ],
    ids=[
        "other-doctype",
        "cut-header",
        "no-doctype",
        "unknown-size",
        "read-version",
        "id-length",
        "size-length",
        "child-past-header",
    ],
)
def test_bad_ebml_header_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    container: str,
    reason: str,
) -> None:
    """An invalid or foreign EBML header is unknown, not its extension."""
    _stub_ffprobe(monkeypatch, _ok(VIDEO_AND_AUDIO))
    target = tmp_path / "x.mkv"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to(container)
    assert_that(decision(result).detail).contains(reason)


@needs_ffprobe
@pytest.mark.parametrize(
    ("args", "name", "expected"),
    [
        ((*AUDIO_48K_IN, "-c:a", "libopus"), "tone.webm", MediaType.AUDIO),
        ((*AUDIO_IN, "-c:a", "flac"), "tone.mka", MediaType.AUDIO),
        ((*VIDEO_IN, "-c:v", "libvpx"), "clip.webm", MediaType.VIDEO),
        ((*VIDEO_IN, "-c:v", "mpeg4"), "clip.mkv", MediaType.VIDEO),
    ],
    ids=["opus-webm", "flac-mka", "vp8-webm", "mpeg4-mkv"],
)
def test_real_matroska_files(
    tmp_path: Path,
    args: tuple[str, ...],
    name: str,
    expected: MediaType,
) -> None:
    """ffmpeg-written Matroska and WebM are typed by their streams."""
    target = tmp_path / name
    run_ffmpeg(*args, str(target))
    renamed = target.rename(tmp_path / "renamed.bin")

    assert_that(detect(renamed).media_type).is_equal_to(expected)


# --- MPEG transport streams --------------------------------------------------

_PMT_PID = 0x1000


def _packet(pid: int, payload: bytes, *, unit_start: bool = True) -> bytes:
    """Build one 188-byte transport packet with a payload.

    Args:
        pid: Packet identifier.
        payload: Payload bytes (padded with ``FF``).
        unit_start: Payload unit start indicator.

    Returns:
        The packet.
    """
    header = bytes([0x47, (0x40 if unit_start else 0) | (pid >> 8), pid & 0xFF, 0x10])
    return header + payload + b"\xff" * (184 - len(payload))


def _section(table_id: int, body: bytes) -> bytes:
    """Build a PSI section with a dummy CRC, behind a zero pointer field.

    Args:
        table_id: Table ID.
        body: Bytes after the section length.

    Returns:
        Pointer field and section.
    """
    length = len(body) + 4
    return (
        b"\x00"
        + bytes([table_id, 0xB0 | (length >> 8), length & 0xFF])
        + body
        + b"\x00" * 4
    )


def _transport_stream(
    *streams: tuple[int, bytes], stride: int = 188, null: int = 6
) -> bytes:
    """Build a transport stream with a PAT, a PMT and null packets.

    Args:
        streams: ``(stream_type, descriptors)`` of each elementary stream.
        stride: 188 for TS, 192 for BDAV (a four-byte prefix per packet).
        null: Null packets after the tables.

    Returns:
        The stream.
    """
    pat = _section(
        0x00,
        b"\x00\x01\xc1\x00\x00" + b"\x00\x01" + struct.pack(">H", 0xE000 | _PMT_PID),
    )
    loop = b"".join(
        bytes([kind, 0xE1, 0x01 + index, 0xF0 | (len(desc) >> 8), len(desc) & 0xFF])
        + desc
        for index, (kind, desc) in enumerate(streams)
    )
    pmt = _section(0x02, b"\x00\x01\xc1\x00\x00\xe1\x00\xf0\x00" + loop)
    packets = [_packet(0, pat), _packet(_PMT_PID, pmt)]
    packets += [_packet(0x1FFF, b"") for _ in range(null)]
    prefix = b"\x00\x00\x00\x00" if stride == 192 else b""
    return b"".join(prefix + packet for packet in packets)


@pytest.mark.parametrize(
    ("streams", "expected"),
    [
        (((0x1B, b""), (0x0F, b"")), MediaType.VIDEO),
        (((0x0F, b""),), MediaType.AUDIO),
        (((0x03, b""), (0x81, b"")), MediaType.AUDIO),
        (((0x06, b"\x6a\x01\x00"),), MediaType.AUDIO),
        (((0x06, b"\x05\x04Opus"),), MediaType.AUDIO),
        (((0x0F, b""), (0x06, b"\x59\x00")), MediaType.AUDIO),
        (((0x24, b""), (0x86, b"")), MediaType.VIDEO),
        (((0x05, b""),), None),
    ],
    ids=[
        "avc-aac",
        "aac-only",
        "mp2-ac3",
        "dvb-ac3",
        "opus",
        "aac-subtitles",
        "hevc-unknown",
        "data-only",
    ],
)
@pytest.mark.parametrize("stride", [188, 192], ids=["ts", "m2ts"])
def test_pmt_stream_types_decide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streams: tuple[tuple[int, bytes], ...],
    expected: MediaType | None,
    stride: int,
) -> None:
    """The PMT's stream types, not the sync pattern, decide TS and M2TS."""
    _stub_ffprobe(monkeypatch, None)
    target = tmp_path / "stream.bin"
    target.write_bytes(_transport_stream(*streams, stride=stride))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_equal_to("mpeg-ts" if stride == 188 else "m2ts")


def test_m2ts_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M2TS sync bytes sit every 192 bytes from offset 4."""
    _stub_ffprobe(monkeypatch, None)
    target = tmp_path / "00001.m2ts"
    target.write_bytes(_transport_stream((0x1B, b""), stride=192))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(details(result)).contains("sync=0x47 every 192 bytes from offset 4")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ((b"\x47" + b"\x00" * 187) * 2 + b"\x00" * 100, MediaType.AUDIO),
        (b"\x47" + b"\x00" * 600, None),
    ],
    ids=["two-packets-short-file", "one-sync-byte"],
)
def test_sync_points_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    expected: MediaType | None,
) -> None:
    """Sync bytes count only in a row; a single ``47`` is not a stream."""
    _stub_ffprobe(monkeypatch, _ok(AUDIO_ONLY))
    target = tmp_path / "x.bin"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)


@pytest.mark.parametrize(
    ("run", "expected", "reason"),
    [
        (None, None, "unknown unavailable: program tables incomplete"),
        (_ok(AUDIO_ONLY), MediaType.AUDIO, "stream types audio from ffprobe"),
        (_ok(b'{"streams": []}'), None, "unknown unresolved"),
    ],
    ids=["no-ffprobe", "ffprobe-audio", "ffprobe-nothing"],
)
def test_ts_without_tables_asks_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run: BoundedRun | None,
    expected: MediaType | None,
    reason: str,
) -> None:
    """Without a PAT and PMT in the bytes read, ffprobe decides or nothing does."""
    _stub_ffprobe(monkeypatch, run)
    target = tmp_path / "x.ts"
    target.write_bytes(b"".join(_packet(0x1FFF, b"") for _ in range(8)))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(decision(result).detail).contains(reason)


def test_pmt_split_across_packets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PMT section continued in a second packet is assembled."""
    _stub_ffprobe(monkeypatch, None)
    language = b"\x0a\x04eng\x00"
    loop = b"".join(
        bytes([0x0F, 0xE1, 0x01 + index, 0xF0, len(language * 6)]) + language * 6
        for index in range(5)
    )
    pmt = _section(0x02, b"\x00\x01\xc1\x00\x00\xe1\x00\xf0\x00" + loop)
    assert_that(len(pmt)).is_greater_than(184)
    pat = _section(
        0x00, b"\x00\x01\xc1\x00\x00\x00\x01" + struct.pack(">H", 0xE000 | _PMT_PID)
    )
    packets = [
        _packet(0, pat),
        _packet(_PMT_PID, pmt[:184]),
        _packet(_PMT_PID, pmt[184:], unit_start=False),
        *[_packet(0x1FFF, b"") for _ in range(4)],
    ]
    target = tmp_path / "x.ts"
    target.write_bytes(b"".join(packets))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(decision(result).detail).contains("pmt audio streams")


def test_classify_stream_descriptors() -> None:
    """Private PES streams are classified by their descriptors."""
    assert_that(classify_stream(0x06, b"\x7a\x00")).is_equal_to("audio")
    assert_that(classify_stream(0x06, b"\x05\x04BSSD")).is_equal_to("audio")
    assert_that(classify_stream(0x06, b"\x56\x00")).is_equal_to("data")
    assert_that(classify_stream(0x06, b"\x05\x04ZZZZ")).is_none()
    assert_that(classify_stream(0x06, b"")).is_none()
    assert_that(classify_stream(0x80, b"")).is_none()


@needs_ffprobe
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((*AUDIO_IN, "-c:a", "aac", "-f", "mpegts"), MediaType.AUDIO),
        (
            (*AUDIO_IN, "-c:a", "mp2", "-f", "mpegts", "-mpegts_m2ts_mode", "1"),
            MediaType.AUDIO,
        ),
        ((*AUDIO_48K_IN, "-c:a", "libopus", "-f", "mpegts"), MediaType.AUDIO),
        ((*VIDEO_IN, "-c:v", "mpeg2video", "-f", "mpegts"), MediaType.VIDEO),
    ],
    ids=["aac-ts", "mp2-m2ts", "opus-ts", "mpeg2-ts"],
)
def test_real_transport_streams(
    tmp_path: Path,
    args: tuple[str, ...],
    expected: MediaType,
) -> None:
    """ffmpeg-written transport streams are typed by their streams.

    The BDAV writer tags MPEG audio as private data without a descriptor, so
    that PMT does not decide and ffprobe does.
    """
    target = tmp_path / "stream.bin"
    run_ffmpeg(*args, str(target))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(
        [d for d in details(result) if d.startswith("pmt stream_type=")]
    ).is_not_empty()


# --- dispatch: audio typed from these containers reaches the audio reader ----


@needs_ffprobe
@pytest.mark.parametrize(
    ("args", "name"),
    [
        ((*AUDIO_48K_IN, "-c:a", "libopus"), "tone.webm"),
        ((*AUDIO_IN, "-c:a", "aac", "-f", "mpegts"), "tone.ts"),
        ((*AUDIO_IN, "-c:a", "aac"), "tone.mp4"),
    ],
    ids=["webm", "ts", "mp4"],
)
def test_audio_only_containers_extract_a_duration(
    tmp_path: Path,
    args: tuple[str, ...],
    name: str,
) -> None:
    """Audio-only WebM, TS and MP4 are audio and get a duration."""
    target = tmp_path / name
    run_ffmpeg(*args, str(target))

    metadata = DefaultMetadataService().extract(target)

    assert_that(detect(target).media_type).is_equal_to(MediaType.AUDIO)
    assert_that(metadata.duration_seconds).is_not_none()


@needs_ffprobe
@pytest.mark.parametrize("name", ["mp2.m2ts", "opus.mka", "ac3.mka"])
def test_audio_fixtures_in_streams_extract_a_duration(name: str) -> None:
    """Committed audio-only M2TS and Matroska fixtures get a duration."""
    metadata = DefaultMetadataService().extract(AUDIO_FIXTURES / name)

    assert_that(metadata.duration_seconds).is_not_none()


def test_missing_ffprobe_is_an_explicit_unknown_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio typed from its PMT, with no ffprobe, says the reader is missing."""
    _stub_ffprobe(monkeypatch, None)
    monkeypatch.setattr(audio, "find_binary", lambda _: None)
    target = tmp_path / "tone.ts"
    target.write_bytes(_transport_stream((0x0F, b"")))

    metadata = DefaultMetadataService().extract(target)

    assert_that(detect(target).media_type).is_equal_to(MediaType.AUDIO)
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.audio_flags or ()).contains(AudioFlag.BACKEND_UNAVAILABLE)
    assert_that(metadata.audio_backend_reason).is_equal_to("ffprobe not found on PATH")
