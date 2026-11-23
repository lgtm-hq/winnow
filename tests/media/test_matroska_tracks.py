"""Tests for native Matroska and WebM track types (#393, rule 3).

The small files are built by ``tests.media.ebml_support`` and run with
ffprobe stubbed out, so they hold in CI without FFmpeg. ffmpeg-written files
are skipped without it, and also run with ffprobe stubbed out.
"""

from __future__ import annotations

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
    run_ffmpeg,
)
from tests.media.ebml_support import (
    AUDIO,
    COMPLEX,
    INFO,
    SUBTITLE,
    TRACK_ENTRY,
    TRACK_NUMBER,
    TRACKS,
    VIDEO,
    VOID,
    cluster,
    ebml_header,
    element,
    matroska,
    seek_head,
    tracks,
    tracks_after_cluster,
)
from winnow.media import _matroska_tracks, _stream_probe
from winnow.media._detect import DetectionResult, detect
from winnow.media._ffprobe import BoundedRun
from winnow.models.media import MediaType

_AUDIO_ONLY = b'{"streams": [{"index": 0, "codec_type": "audio"}]}'


@pytest.fixture
def no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ffprobe absent for stream-type probes.

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


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize(
    ("data", "name", "expected", "found"),
    [
        (matroska(tracks(VIDEO, AUDIO), cluster()), "clip.mkv", VIDEO, "video,audio"),
        (
            matroska(tracks(AUDIO, VIDEO), cluster(), doctype=b"webm"),
            "clip.webm",
            VIDEO,
            "audio,video",
        ),
        (
            matroska(tracks(AUDIO), cluster(), doctype=b"webm"),
            "tone.webm",
            AUDIO,
            "audio",
        ),
        (matroska(tracks(AUDIO, AUDIO), cluster()), "tone.mka", AUDIO, "audio,audio"),
        (
            matroska(tracks(AUDIO, SUBTITLE), cluster()),
            "tone.bin",
            AUDIO,
            "audio,subtitle",
        ),
        (matroska(tracks(SUBTITLE), cluster()), "subs.mks", None, "subtitle"),
        (matroska(tracks(), cluster()), "empty.mkv", None, "(none)"),
    ],
    ids=[
        "mkv-video",
        "webm-video",
        "webm-audio",
        "mka-audio",
        "audio-subs",
        "subs",
        "empty",
    ],
)
def test_native_track_types_decide(
    tmp_path: Path,
    data: bytes,
    name: str,
    expected: int | None,
    found: str,
) -> None:
    """Track types read from the file decide without ffprobe."""
    types = {VIDEO: MediaType.VIDEO, AUDIO: MediaType.AUDIO}

    result = _detect_bytes(tmp_path, data, name)

    assert_that(result.media_type).is_equal_to(types.get(expected or 0))
    assert_that(result.container).is_equal_to("matroska")
    assert_that(details(result)).contains(f"tracks={found}")
    assert_that(decision(result).detail).does_not_contain("ffprobe")


def test_native_walk_does_not_call_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled native walk never starts ffprobe."""

    def _forbidden(*_: object, **__: object) -> None:
        raise AssertionError("ffprobe was asked")

    monkeypatch.setattr("winnow.media._matroska.probe_stream_types", _forbidden)

    result = _detect_bytes(tmp_path, matroska(tracks(AUDIO), cluster()))

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(decision(result).detail).is_equal_to(
        "decision=matroska track types audio"
    )


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize("cluster_bytes", [64, 1024 * 1024])
def test_tracks_after_a_cluster_found_through_seekhead(
    tmp_path: Path,
    cluster_bytes: int,
) -> None:
    """``Tracks`` after a ``Cluster`` is reached through the ``SeekHead``.

    The 1 MiB ``Cluster`` is over the read budget, so it is never read.
    """
    result = _detect_bytes(tmp_path, tracks_after_cluster(cluster_bytes), "late.mka")

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(details(result)).contains("tracks=audio")


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize(
    ("data", "expected", "reason"),
    [
        (
            matroska(tracks(VIDEO), cluster(unknown=True), unknown=True),
            MediaType.VIDEO,
            "",
        ),
        (
            matroska(
                element(INFO, b"\x00"),
                tracks(AUDIO),
                cluster(unknown=True),
                unknown=True,
            ),
            MediaType.AUDIO,
            "",
        ),
        (
            matroska(cluster(unknown=True), tracks(AUDIO), unknown=True),
            None,
            "no Tracks before a Cluster",
        ),
    ],
    ids=["tracks-then-live-cluster", "info-tracks-live", "live-cluster-first"],
)
def test_live_stream_unknown_sizes(
    tmp_path: Path,
    data: bytes,
    expected: MediaType | None,
    reason: str,
) -> None:
    """Unknown-size Segment and Cluster elements, as live streams write them."""
    result = _detect_bytes(tmp_path, data, "live.webm")

    assert_that(result.media_type).is_equal_to(expected)
    if reason:
        assert_that(details(result)).contains(f"tracks=unresolved: {reason}")
        assert_that(decision(result).detail).contains("ffprobe needed")


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize(
    "data",
    [
        matroska(tracks(VIDEO, AUDIO), cluster(16)),
        tracks_after_cluster(48),
        matroska(tracks(AUDIO), cluster(8, unknown=True), unknown=True),
    ],
    ids=["plain", "seekhead", "live"],
)
def test_truncated_at_every_byte(tmp_path: Path, data: bytes) -> None:
    """A file cut at any byte gives a result, never an exception or a wrong type."""
    full = _detect_bytes(tmp_path, data).media_type
    target = tmp_path / "cut.bin"

    for length in range(len(data)):
        target.write_bytes(data[:length])
        result = detect(target)
        assert_that(result.media_type).described_as(str(length)).is_in(None, full)
        if result.media_type is None and length > 4:
            assert_that(result.container).described_as(str(length)).is_not_none()


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (ebml_header() + element(INFO), "no Segment after the EBML header"),
        (
            matroska(element(INFO, b"")[:4] + b"\x01\xff\xff\xff\xff\xff\xff\xff"),
            "unknown size outside",
        ),
        (
            matroska(
                element(TRACKS, element(TRACK_ENTRY, element(TRACK_NUMBER, b"\x01")))
            ),
            "TrackEntry without TrackType",
        ),
        (
            matroska(
                element(TRACKS, element(TRACK_ENTRY, element(bytes([0x83]), b"")))
            ),
            "TrackType is not an unsigned integer",
        ),
        (matroska(seek_head(TRACKS, 4096), cluster()), "SeekHead points outside"),
        (matroska(seek_head(TRACKS, 0), cluster()), "not a Tracks element"),
        (
            matroska(
                element(
                    TRACKS,
                    element(TRACK_ENTRY, b"\x83\x01\xff\xff\xff\xff\xff\xff\xff"),
                )
            ),
            "child element of unknown size",
        ),
        (
            matroska(element(TRACKS, element(TRACK_ENTRY, b"\x83\x85\x01"))),
            "past its parent",
        ),
        (matroska(b"\x08\x00\x00\x00\x00\x81\x00"), "invalid element ID"),
        (matroska(b"\x00\x81\x00"), "invalid element ID"),
    ],
    ids=[
        "no-segment",
        "unknown-size-info",
        "no-track-type",
        "empty-track-type",
        "seek-outside",
        "seek-not-tracks",
        "child-unknown-size",
        "child-past-parent",
        "five-byte-id",
        "zero-id-byte",
    ],
)
def test_malformed_input_is_unknown_with_a_signal(
    tmp_path: Path,
    data: bytes,
    reason: str,
) -> None:
    """Malformed input is unknown with a signal, never the extension."""
    result = _detect_bytes(tmp_path, data, "broken.mkv")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("matroska")
    malformed = [
        d for d in details(result) if d.startswith("tracks=unresolved malformed")
    ]
    assert_that(malformed).is_length(1)
    assert_that(malformed[0]).contains(reason)
    assert_that(details(result)).contains("unavailable=ffprobe not found on PATH")


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize(
    ("data", "constant", "value", "reason"),
    [
        (
            matroska(element(VOID) * 40, tracks(AUDIO)),
            "MAX_ELEMENTS",
            16,
            "element count",
        ),
        (
            matroska(element(VOID, b"\x00" * 64), tracks(AUDIO)),
            "MAX_BYTES_READ",
            40,
            "read budget",
        ),
        (
            matroska(tracks(AUDIO, VIDEO)),
            "MAX_MASTER_BYTES",
            8,
            "master element larger than its read budget",
        ),
        (matroska(tracks(AUDIO)), "MAX_DEPTH", 2, "element depth"),
    ],
    ids=["elements", "bytes", "master", "depth"],
)
def test_caps_stop_the_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    constant: str,
    value: int,
    reason: str,
) -> None:
    """Each cap ends the walk as over budget, never as an answer."""
    monkeypatch.setattr(_matroska_tracks, constant, value)

    result = _detect_bytes(tmp_path, data, "x.mka")

    assert_that(result.media_type).is_none()
    assert_that(details(result)).contains(
        f"tracks=unresolved budget-exceeded: {reason}"
    )


@pytest.mark.parametrize(
    ("data", "stdout", "expected"),
    [
        (matroska(tracks(COMPLEX), cluster()), _AUDIO_ONLY, MediaType.AUDIO),
        (matroska(cluster(), tracks(AUDIO)), _AUDIO_ONLY, MediaType.AUDIO),
    ],
    ids=["complex-track", "tracks-not-found"],
)
def test_unsettled_walk_asks_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    stdout: bytes,
    expected: MediaType,
) -> None:
    """ffprobe decides only what the native walk could not settle."""
    monkeypatch.setattr(
        _stream_probe, "find_binary", lambda _: "/opt/ffmpeg/bin/ffprobe"
    )
    monkeypatch.setattr(_stream_probe, "fd_protocol_supported", lambda _: True)
    run = BoundedRun(
        returncode=0, stdout=stdout, stderr=b"", timed_out=False, overflowed=False
    )
    monkeypatch.setattr(_stream_probe, "run_bounded", lambda *_, **__: run)

    result = _detect_bytes(tmp_path, data, "x.mkv")

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(decision(result).detail).contains("from ffprobe")
    assert_that([d for d in details(result) if d.startswith("tracks=")]).is_length(1)


@pytest.mark.usefixtures("no_ffprobe")
def test_complex_track_without_ffprobe_is_unknown(tmp_path: Path) -> None:
    """A complex track alone does not say whether the file has video."""
    result = _detect_bytes(tmp_path, matroska(tracks(COMPLEX), cluster()), "x.mkv")

    assert_that(result.media_type).is_none()
    assert_that(details(result)).contains("tracks=complex unresolved: complex track")


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize(
    ("args", "name", "expected"),
    [
        ((*AUDIO_48K_IN, "-c:a", "libopus"), "tone.webm", MediaType.AUDIO),
        ((*AUDIO_IN, "-c:a", "flac"), "tone.mka", MediaType.AUDIO),
        ((*VIDEO_IN, "-c:v", "libvpx"), "clip.webm", MediaType.VIDEO),
        (
            (*VIDEO_IN, *AUDIO_IN, "-c:v", "mpeg4", "-c:a", "flac"),
            "clip.mkv",
            MediaType.VIDEO,
        ),
        (
            (
                *AUDIO_48K_IN,
                "-c:a",
                "libopus",
                "-live",
                "1",
                "-cluster_size_limit",
                "4096",
            ),
            "live.webm",
            MediaType.AUDIO,
        ),
    ],
    ids=["opus-webm", "flac-mka", "vp8-webm", "mpeg4-mkv", "live-opus"],
)
def test_ffmpeg_written_matroska_without_ffprobe(
    tmp_path: Path,
    args: tuple[str, ...],
    name: str,
    expected: MediaType,
) -> None:
    """ffmpeg-written Matroska and WebM are typed with ffprobe absent."""
    written = tmp_path / name
    run_ffmpeg(*args, str(written))

    result = detect(written.rename(tmp_path / "renamed.bin"))

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(decision(result).detail).starts_with("decision=matroska track types")


@pytest.mark.usefixtures("no_ffprobe")
@pytest.mark.parametrize(
    "name",
    sorted(path.name for path in AUDIO_FIXTURES.glob("*.mk[av]")),
)
def test_matroska_fixtures_without_ffprobe(name: str) -> None:
    """Every committed Matroska fixture is typed natively."""
    result = detect(AUDIO_FIXTURES / name)

    expected = MediaType.VIDEO if name.endswith(".mkv") else MediaType.AUDIO
    assert_that(result.media_type).is_equal_to(expected)
