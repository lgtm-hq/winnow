"""Regression tests for the automated review of pull request 454, and fuzzing.

Two rules hold for every input: nothing raises out of ``detect``, and audio
is decided only from a complete stream list. Each review finding has a test
that failed before its fix. ffprobe is stubbed out, so a walk that cannot
settle the type gives unknown.
"""

from __future__ import annotations

import hashlib
import struct
import time
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.bmff_support import box, ftyp, moov, track
from tests.media.ebml_support import (
    AUDIO as MKV_AUDIO,
)
from tests.media.ebml_support import (
    VIDEO as MKV_VIDEO,
)
from tests.media.ebml_support import (
    cluster,
    matroska,
    tracks,
    tracks_after_cluster,
)
from tests.media.library_support import LIBRARY_SAMPLES, PLAYLIST_SAMPLES
from tests.media.signature_support import (
    OPUS_HEAD,
    THEORA_ID,
    ac3_frames,
    adts_frames,
    asf,
    avi,
    dts_frames,
    flv,
    id3v2,
    mpeg_frames,
    ogg_page,
    program_stream,
    realmedia,
    riff,
    riff_chunk,
)
from tests.media.test_signatures import _HEADERS
from winnow.media import _stream_probe
from winnow.media._detect import TRACK_BOX, DetectionResult, detect
from winnow.media._signatures import HEAD_BYTES
from winnow.models.media import MediaType


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ffprobe absent, so only the bytes decide.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)


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


SWEEP_LIMIT_SECONDS = 2.0
"""Longest any single ``detect`` call in the sweeps may take."""


def _timed_detect(target: Path) -> DetectionResult:
    """Detect ``target`` and fail when it takes longer than the sweep limit.

    Args:
        target: File to detect.

    Returns:
        The result.
    """
    start = time.perf_counter()
    result = detect(target)
    elapsed = time.perf_counter() - start
    assert_that(elapsed).described_as(f"detect({target.name})").is_less_than(
        SWEEP_LIMIT_SECONDS,
    )
    return result


# --- the seven findings --------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"fLaC", b"fLaC\x00\x00", id3v2(16) + b"fLaC"],
    ids=["bare-marker", "cut-block-header", "after-id3"],
)
def test_cut_flac_marker_does_not_raise(tmp_path: Path, data: bytes) -> None:
    """Finding 1: ``fLaC`` without a whole block header is not FLAC, no error."""
    result = _detect_bytes(tmp_path, data)

    assert_that(result.media_type).is_none()


def test_netpbm_with_a_huge_number_does_not_raise(tmp_path: Path) -> None:
    """Finding 2: a 5,000-digit dimension is not Netpbm and raises nothing."""
    data = b"P6\n" + b"9" * 5000 + b" 1\n255\n" + b"\x00" * 16

    result = _detect_bytes(tmp_path, data, "x.ppm")

    assert_that(result.container).is_not_equal_to("netpbm")


@pytest.mark.parametrize(
    "audio_offset", [HEAD_BYTES - 36, HEAD_BYTES - 500, HEAD_BYTES + 8]
)
def test_id3_tag_ending_near_the_head_end(tmp_path: Path, audio_offset: int) -> None:
    """Finding 3: frames after a tag that ends near 64 KiB are still read."""
    data = id3v2(audio_offset - 10) + mpeg_frames(4)

    result = _detect_bytes(tmp_path, data)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(result.container).is_equal_to("mpeg-audio")


def _avi_with_late_video() -> bytes:
    """Build an AVI whose video ``strh`` lies past the 64 KiB head.

    Returns:
        The file bytes.
    """
    audio = riff_chunk(b"LIST", b"strl" + riff_chunk(b"strh", b"auds" + b"\x00" * 52))
    junk = riff_chunk(b"JUNK", b"\x00" * (HEAD_BYTES + 1024))
    video = riff_chunk(b"LIST", b"strl" + riff_chunk(b"strh", b"vids" + b"\x00" * 52))
    hdrl = riff_chunk(
        b"LIST", b"hdrl" + riff_chunk(b"avih", b"\x00" * 56) + audio + junk + video
    )
    return riff(b"RIFF", b"AVI ", hdrl + riff_chunk(b"LIST", b"movi"))


def _asf_with_late_video() -> bytes:
    """Build an ASF whose second Stream Properties lies past the head.

    Returns:
        The file bytes.
    """
    data = asf("audio", "video")
    first_end = 30 + 78
    padding = HEAD_BYTES + 1024
    # A padding object (unknown GUID) between the two streams.
    pad = b"\x11" * 16 + struct.pack("<Q", 24 + padding) + b"\x00" * padding
    body = data[30:first_end] + pad + data[first_end:]
    return data[:16] + struct.pack("<Q", 30 + len(body)) + data[24:30] + body


def _ogg_with_late_video() -> bytes:
    """Build an Ogg whose Theora BOS page lies past the head.

    Returns:
        The file bytes.
    """
    audio = b"".join(
        ogg_page(OPUS_HEAD + b"\x00" * 250, serial=index) for index in range(300)
    )
    late = ogg_page(THEORA_ID, serial=300) + ogg_page(b"\x00" * 8, bos=False)
    return audio + late


def _program_stream_with_late_video() -> bytes:
    """Build a program stream whose video packet lies past the head.

    Returns:
        The file bytes.
    """
    audio = [(0xC0, b"\x00" * 60_000), (0xC0, b"\x00" * 10_000)]
    return program_stream(*audio, (0xE0, b"\x00" * 64))


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (_avi_with_late_video(), "clip.avi"),
        (_asf_with_late_video(), "clip.wmv"),
        (_ogg_with_late_video(), "clip.ogv"),
    ],
    ids=["avi", "asf", "ogg"],
)
def test_incomplete_stream_list_never_decides_audio(
    tmp_path: Path,
    data: bytes,
    name: str,
) -> None:
    """Finding 4: a stream list cut by the bytes read is not proof of audio."""
    result = _detect_bytes(tmp_path, data, name)

    assert_that(result.media_type).is_not_equal_to(MediaType.AUDIO)
    assert_that(result.media_type).is_none()
    assert_that([d for d in _details(result) if d == "streams=incomplete"]).is_length(1)


def test_program_stream_video_past_the_head_is_read(tmp_path: Path) -> None:
    """The program stream walk reads headers past the head and finds video."""
    result = _detect_bytes(tmp_path, _program_stream_with_late_video(), "clip.mpg")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def _details(result: DetectionResult) -> list[str]:
    """Return every signal detail.

    Args:
        result: Detection result.

    Returns:
        The details.
    """
    return [signal.detail for signal in result.signals]


def _ts_packet(pid: int, section: bytes) -> bytes:
    """Build one transport packet holding a PSI section.

    Args:
        pid: Packet identifier.
        section: Section bytes, pointer field excluded.

    Returns:
        The packet.
    """
    payload = b"\x00" + section
    return (
        bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
        + payload
        + b"\xff" * (184 - len(payload))
    )


def _psi(
    table_id: int,
    body: bytes,
    *,
    number: int = 0,
    last: int = 0,
    extension: int = 1,
    current: bool = True,
) -> bytes:
    """Build a long-form PSI section with a dummy CRC.

    Args:
        table_id: Table ID.
        body: Bytes after ``last_section_number``.
        number: ``section_number``.
        last: ``last_section_number``.
        extension: ``transport_stream_id`` or ``program_number``.
        current: ``current_next_indicator``.

    Returns:
        The section.
    """
    version = 0xC1 if current else 0xC0
    rest = extension.to_bytes(2, "big") + bytes([version, number, last]) + body
    rest += b"\x00" * 4
    return bytes([table_id, 0xB0 | (len(rest) >> 8), len(rest) & 0xFF]) + rest


def test_multi_section_pat_waits_for_every_section(tmp_path: Path) -> None:
    """Finding 5: a program named in a later PAT section is still read."""
    audio_pmt = _psi(0x02, b"\xe1\x00\xf0\x00\x0f\xe1\x01\xf0\x00")
    video_pmt = _psi(0x02, b"\xe2\x00\xf0\x00\x1b\xe2\x01\xf0\x00", extension=2)
    packets = [
        _ts_packet(0, _psi(0x00, b"\x00\x01\xf0\x00", number=0, last=1)),
        _ts_packet(0x1000, audio_pmt),
        _ts_packet(0, _psi(0x00, b"\x00\x02\xf0\x10", number=1, last=1)),
        _ts_packet(0x1010, video_pmt),
        *[_ts_packet(0x1FFF, b"") for _ in range(4)],
    ]

    result = _detect_bytes(tmp_path, b"".join(packets), "x.ts")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)


def test_broken_track_inside_legacy_moov_is_malformed(tmp_path: Path) -> None:
    """Finding 6: a broken track inside ``moov`` is not hidden by the tail guard."""
    broken = box(TRACK_BOX, box(b"mdia", box(b"hdlr", b"\x00" * 4)))
    data = box(b"moov", box(b"mvhd", b"\x00" * 16) + track("soun") + broken)

    result = _detect_bytes(tmp_path, data, "old.mov")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("quicktime")
    assert_that(
        next(d for d in _details(result) if d.startswith("decision="))
    ).contains(
        "malformed: hdlr shorter",
    )


@pytest.mark.parametrize(
    "frames",
    [mpeg_frames(3), adts_frames(3), ac3_frames(3), dts_frames(3)],
    ids=["mpeg", "adts", "ac3", "dts"],
)
def test_truncated_third_frame_is_not_audio(tmp_path: Path, frames: bytes) -> None:
    """Finding 7: a third frame cut by the end of the file does not count."""
    result = _detect_bytes(tmp_path, frames[:-1])

    assert_that(result.media_type).is_none()


# --- nothing raises ------------------------------------------------------------

_SMALL_FILES: dict[str, bytes] = {
    **{f"header-{name}": data for name, data, _, _ in _HEADERS},
    "avi": avi(b"vids", b"auds"),
    "asf": asf("audio", "video"),
    "flv": flv(0x05, 18, 8, 9),
    "realmedia": realmedia(b"audio/x-pn-realaudio"),
    "mpeg-ps": program_stream((0xC0, b"\x00" * 8), video_bound=0),
    "ogg": ogg_page(OPUS_HEAD) + ogg_page(b"\x00" * 8, bos=False),
    "id3-mp3": id3v2(32) + mpeg_frames(),
    "id3-flac": id3v2(8) + b"fLaC\x00\x00\x00\x22" + b"\x00" * 34,
    "bmff-movie": ftyp("isom", ("isom",)) + moov(("soun", "vide")),
    "matroska": matroska(tracks(MKV_VIDEO, MKV_AUDIO), cluster()),
    "matroska-seek": tracks_after_cluster(64),
    "matroska-live": matroska(
        tracks(MKV_AUDIO), cluster(8, unknown=True), unknown=True
    ),
    **{f"library-{name}": data for name, _, data, _, _ in LIBRARY_SAMPLES},
    **{f"playlist-{name}": data for name, _, data in PLAYLIST_SAMPLES},
}


@pytest.mark.parametrize("name", sorted(_SMALL_FILES))
def test_every_prefix_of_a_small_file_is_safe(tmp_path: Path, name: str) -> None:
    """Every prefix of each small test file is detected without an exception."""
    data = _SMALL_FILES[name]
    full = _detect_bytes(tmp_path, data).media_type
    target = tmp_path / "prefix.bin"

    for length in range(len(data) + 1):
        target.write_bytes(data[:length])
        result = _timed_detect(target)
        assert_that(result.media_type).described_as(f"{name}[:{length}]").is_in(
            None, full
        )


_FUZZ_SEED = 393
_FUZZ_PER_SIGNATURE = 100


@pytest.mark.parametrize("name", sorted(_SMALL_FILES))
def test_random_bytes_after_each_signature_are_safe(tmp_path: Path, name: str) -> None:
    """Random bytes behind each signature never make ``detect`` raise."""
    data = _SMALL_FILES[name]
    target = tmp_path / "fuzz.bin"

    for case in range(_FUZZ_PER_SIGNATURE):
        noise = _noise(f"{_FUZZ_SEED}-{name}-{case}")
        keep = 1 + noise[0] % max(1, min(len(data), 64))
        tail = noise[2 : 2 + int.from_bytes(noise[:2], "big") % 2048]
        target.write_bytes(data[:keep] + tail)
        _timed_detect(target)


def _noise(seed: str) -> bytes:
    """Return 2,050 deterministic pseudo-random bytes for ``seed``.

    Args:
        seed: Any text.

    Returns:
        The bytes, from chained SHA-256 digests.
    """
    out = b""
    block = seed.encode()
    while len(out) < 2050:
        block = hashlib.sha256(block).digest()
        out += block
    return out[:2050]


def _deep_inputs() -> dict[str, bytes]:
    """Build deeply nested inputs for every container that nests.

    Returns:
        Name to file bytes.
    """
    from tests.media.test_detect_hardening import _deep_asf, _deep_avi

    nested = box(TRACK_BOX, box(b"mdia", b""))
    for _ in range(1100):
        nested = box(b"moov", nested)
    entry = b""
    for _ in range(300):
        entry = b"\xae" + _ebml_size(len(entry)) + entry
    return {
        "avi-strl": _deep_avi(1100),
        "asf-extension": _deep_asf(1100),
        "bmff-moov": ftyp("isom", ("isom",)) + nested,
        "matroska-entries": matroska(
            b"\x16\x54\xae\x6b" + _ebml_size(len(entry)) + entry
        ),
    }


def _ebml_size(value: int) -> bytes:
    """Encode an EBML data size in eight bytes.

    Args:
        value: The size.

    Returns:
        The VINT.
    """
    return (0x01 << 56 | value).to_bytes(8, "big")


@pytest.mark.parametrize(
    "name", ["avi-strl", "asf-extension", "bmff-moov", "matroska-entries"]
)
def test_deep_nesting_prefixes_are_safe(tmp_path: Path, name: str) -> None:
    """Deep nesting, whole and cut every 97 bytes, never raises."""
    data = _deep_inputs()[name]
    target = tmp_path / "deep.bin"

    for length in [*range(0, len(data), 97), len(data)]:
        target.write_bytes(data[:length])
        assert_that(_timed_detect(target).media_type).described_as(
            f"{name}[:{length}]"
        ).is_in(
            None,
            MediaType.VIDEO,
        )
