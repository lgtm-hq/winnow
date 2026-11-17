"""Fixture tests for the audio reader against the installed ffprobe.

These need a real ffprobe and are skipped when none is installed. Every
reported value is compared with the true value from the fixture generator's
table (the generation parameters), with the tolerances stated below. What
depends on the FFmpeg build (the benign stderr lines, which fixtures carry a
stream bitrate) is pinned per major version; on a major without pins the
tests fail instead of guessing. Truncated and hostile inputs are built here
from the committed fixtures.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from assertpy import assert_that

from tests.media.fixtures.audio.generate_audio_fixtures import (
    CHAINED_OGG,
    FIXTURES,
    FRAME_CUTS,
    FRAME_SOURCES,
    AudioFixture,
    FrameCut,
)
from winnow.media import audio as audio_module
from winnow.media._ffprobe import (
    ffprobe_available,
    ffprobe_version,
    find_binary,
    parse_version_major,
)
from winnow.media.audio import BENIGN_STDERR_BY_MAJOR, extract_audio_metadata
from winnow.media.video import extract_video_metadata
from winnow.models.media import AudioFlag, MediaMetadata

pytestmark = pytest.mark.skipif(not ffprobe_available(), reason="ffprobe not installed")

AUDIO_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures" / "audio"
MEDIA_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures"
HEALTHY: Final[tuple[AudioFixture, ...]] = (*FIXTURES, *FRAME_SOURCES)
BY_NAME: Final[dict[str, AudioFixture]] = {fixture.name: fixture for fixture in HEALTHY}


@dataclass(frozen=True, slots=True)
class Pins:
    """Build-dependent values for one FFmpeg major.

    Args:
        benign: Benign stderr lines.
        stream_bitrate: Fixtures with a declared duration whose primary
            stream has a ``bit_rate`` in this build.
    """

    benign: frozenset[str]
    stream_bitrate: frozenset[str]


PINS: Final[dict[int, Pins]] = {
    9: Pins(
        benign=frozenset(
            {
                "Estimating duration from bitrate, this may be inaccurate",
                "invalid concatenated file detected - using bitrate for duration",
                "ignoring wrong sample_count",
                "Ignoring maximum wav data size, file may be invalid",
                "Unsupported codec with id <N> for input stream <M>",
                "stream 0, timescale not set",
            },
        ),
        stream_bitrate=frozenset(
            {
                "mp3.mp3",
                "wav.wav",
                "aiff.aiff",
                "aac.m4a",
                "wma.wma",
                "alac.m4a",
                "adpcm_ms.wav",
                "cover.mp3",
                "delayed.mp4",
                "cover.m4a",
                "timecode.mov",
                "frames.mp3",
                "frames.m4a",
            },
        ),
    ),
}
"""Build-dependent values per FFmpeg major, measured on 9.0.2."""

ALLOWED_EXTENSIONS: Final[tuple[str, ...]] = (
    ".mp3",
    ".aac",
    ".ac3",
    ".eac3",
    ".flac",
    ".wv",
    ".tta",
    ".wav",
    ".aiff",
    ".m4a",
    ".ogg",
    ".wma",
    ".mka",
    ".avi",
    ".ts",
    ".mpg",
    ".flv",
)
"""One extension per allow-listed demuxer."""

WRONG_EXTENSIONS: Final[tuple[str, ...]] = (".wav", ".flac", ".txt", ".mp4", ".ogg")
DURATION_TOLERANCE: Final[float] = 0.07
"""Reported minus true duration, in seconds, allowed for a healthy fixture.

Encoders add delay and padding that containers count: the largest is
``mp3.flv``, a 22.05 kHz MP3 whose LAME delay and end padding come to about
2.4 frames of 26.1 ms (63 ms).
"""

BITRATE_TOLERANCE: Final[float] = 0.05
"""Relative gap allowed between a stream bitrate and the nominal bitrate.

``aac.m4a`` is 1.4 % under its 24 kbit/s target and ``adpcm_ms.wav`` 0.6 %
over 4 bits per sample (MS ADPCM block headers).
"""


def _installed_major() -> int | None:
    """Read the major version of the installed ffprobe.

    Returns:
        The major version, or ``None`` when it cannot be read.
    """
    binary = find_binary("ffprobe")
    if binary is None:
        return None
    return parse_version_major(ffprobe_version(binary) or "")


@pytest.fixture(scope="module")
def pins() -> Pins:
    """Return the pins for the installed major, failing when there are none.

    Returns:
        The pins for the installed major.
    """
    major = _installed_major()
    if major is None or major not in PINS:
        pytest.fail(f"no audio pins for ffprobe major {major}; measure and add them")
    return PINS[major]


def _duration(metadata: MediaMetadata) -> float:
    """Return the reported duration, failing the test when it is unknown.

    Args:
        metadata: Reader output.

    Returns:
        The duration in seconds.
    """
    if metadata.duration_seconds is None:
        pytest.fail("duration is unknown")
    return metadata.duration_seconds


def _flags(metadata: MediaMetadata) -> tuple[AudioFlag, ...]:
    """Return the audio flags, failing the test when the reader set none.

    Args:
        metadata: Reader output.

    Returns:
        The flags.
    """
    if metadata.audio_flags is None:
        pytest.fail("audio flags were not set")
    return metadata.audio_flags


def _copy(source: Path, directory: Path, name: str) -> Path:
    """Copy a fixture under another name.

    Args:
        source: Fixture file.
        directory: Target directory.
        name: New file name.

    Returns:
        The copy.
    """
    target = directory / name
    shutil.copyfile(source, target)
    return target


def _cut(source: Path, directory: Path, fraction: float) -> Path:
    """Write the first ``fraction`` of a fixture to a new file.

    Args:
        source: Fixture file.
        directory: Target directory.
        fraction: Share of the bytes to keep.

    Returns:
        The cut copy.
    """
    data = source.read_bytes()
    target = directory / f"cut-{fraction}-{source.name}"
    target.write_bytes(data[: int(len(data) * fraction)])
    return target


def test_installed_major_is_pinned(pins: Pins) -> None:
    """The installed major has pins, and they match the reader's list."""
    major = _installed_major()
    if major is None:
        pytest.fail("ffprobe -version has no release number")

    assert_that(BENIGN_STDERR_BY_MAJOR.get(major)).is_equal_to(pins.benign)
    assert_that(pins.stream_bitrate <= set(BY_NAME)).is_true()


@pytest.mark.parametrize("fixture", HEALTHY, ids=lambda fixture: fixture.name)
def test_healthy_fixture_under_wrong_extensions(
    fixture: AudioFixture,
    pins: Pins,
    tmp_path: Path,
) -> None:
    """Each healthy file gives its true facts under three wrong names."""
    source = AUDIO_DIR / fixture.name
    names = [
        f"renamed{suffix}"
        for suffix in WRONG_EXTENSIONS
        if suffix != Path(fixture.name).suffix
    ][:3]

    results = [extract_audio_metadata(_copy(source, tmp_path, name)) for name in names]

    assert_that(results[1]).is_equal_to(results[0])
    assert_that(results[2]).is_equal_to(results[0])
    metadata = results[0]
    assert_that(metadata.container).is_equal_to(fixture.format_name)
    assert_that(metadata.codec).is_equal_to(fixture.codec)
    assert_that(metadata.sample_rate).is_equal_to(fixture.sample_rate)
    assert_that(metadata.channels).is_equal_to(fixture.channels)
    assert_that(metadata.bit_depth).is_equal_to(fixture.bit_depth)
    assert_that(metadata.audio_stream_count).is_equal_to(fixture.audio_streams)
    assert_that(_duration(metadata)).is_close_to(fixture.duration, DURATION_TOLERANCE)
    if fixture.estimated:
        assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
        assert_that(metadata.truncated).is_none()
        assert_that(metadata.bitrate).is_none()
        return
    assert_that(_flags(metadata)).is_empty()
    assert_that(metadata.truncated).is_false()
    if fixture.name not in pins.stream_bitrate:
        assert_that(metadata.bitrate).is_none()
        return
    if metadata.bitrate is None:
        pytest.fail("the stream bitrate is unknown")
    assert_that(metadata.bitrate).is_positive()
    if fixture.bitrate is not None:
        gap = abs(metadata.bitrate - fixture.bitrate) / fixture.bitrate
        assert_that(gap).is_less_than_or_equal_to(BITRATE_TOLERANCE)


@pytest.mark.parametrize(
    ("name", "codec", "channels", "bit_depth"),
    [
        ("mp2_named.mp3", "mp2", 1, None),
        ("alac.m4a", "alac", 1, 16),
        ("adpcm_ms.wav", "adpcm_ms", 1, None),
        ("ac3_mono.ac3", "ac3", 1, None),
        ("flac24.flac", "flac", 1, 24),
    ],
)
def test_previously_mislabelled_codecs(
    name: str,
    codec: str,
    channels: int,
    bit_depth: int | None,
) -> None:
    """Codecs the mutagen path got wrong are reported as ffprobe names them."""
    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that((metadata.codec, metadata.channels, metadata.bit_depth)).is_equal_to(
        (codec, channels, bit_depth),
    )


@pytest.mark.parametrize(
    "name",
    ["vbr_noxing.mp3", "adts.aac", "opus.ogg", "mp2.m2ts"],
)
def test_undeclared_formats_measured_within_50ms(name: str) -> None:
    """VBR MP3 without Xing, ADTS, Ogg, and MPEG-TS are estimated closely."""
    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(_duration(metadata)).is_close_to(BY_NAME[name].duration, 0.05)


@pytest.mark.parametrize("name", ["delayed.mkv", "delayed.mp4"])
def test_delayed_audio_duration(name: str) -> None:
    """Audio that starts late reports its own length, checked (R5)."""
    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that(_duration(metadata)).is_close_to(
        BY_NAME[name].duration,
        DURATION_TOLERANCE,
    )
    assert_that(metadata.truncated).is_false()


@pytest.mark.parametrize("name", ["offset.mka", "offset_delayed.mkv"])
def test_start_offset_is_estimated_not_truncated(name: str) -> None:
    """A4: a Matroska file that starts after zero is measured, never truncated."""
    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(_duration(metadata)).is_close_to(
        BY_NAME[name].duration,
        DURATION_TOLERANCE,
    )


@pytest.mark.parametrize("name", ["cover.m4a", "timecode.mov"])
def test_warned_but_healthy_mp4_is_read(name: str) -> None:
    """A2 and A3: the timecode track and covr art no longer hide the facts."""
    fixture = BY_NAME[name]

    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that((metadata.codec, metadata.sample_rate)).is_equal_to(
        (fixture.codec, fixture.sample_rate),
    )
    assert_that(metadata.audio_stream_count).is_equal_to(1)
    assert_that(metadata.truncated).is_false()
    assert_that(_duration(metadata)).is_close_to(fixture.duration, DURATION_TOLERANCE)


@pytest.mark.parametrize("cut", FRAME_CUTS, ids=lambda cut: cut.name)
def test_last_frames_removed(cut: FrameCut) -> None:
    """Removing the last frames: three are caught, one or two are not.

    The misses are the documented limit: the encoder's end padding is longer
    than the frames removed, so the audio left still reaches the declared
    length within one packet.
    """
    source = BY_NAME[cut.source]

    metadata = extract_audio_metadata(AUDIO_DIR / cut.name)

    assert_that(metadata.codec).is_equal_to(source.codec)
    if cut.detected:
        assert_that(metadata.truncated).is_true()
        assert_that(metadata.duration_seconds).is_none()
    else:
        assert_that(metadata.truncated).is_false()
        assert_that(_duration(metadata)).is_close_to(
            source.duration,
            DURATION_TOLERANCE,
        )


def test_two_tracks_and_cover_art() -> None:
    """The default track is primary; cover art is not a stream (R3)."""
    tracks = extract_audio_metadata(AUDIO_DIR / "two_tracks.mka")
    cover = extract_audio_metadata(AUDIO_DIR / "cover.mp3")

    assert_that((tracks.sample_rate, tracks.channels)).is_equal_to((16000, 2))
    assert_that(tracks.audio_stream_count).is_equal_to(2)
    assert_that(cover.audio_stream_count).is_equal_to(1)
    assert_that(cover.truncated).is_false()


def test_chained_ogg_is_inconsistent() -> None:
    """Chained Ogg has duration unknown and is flagged inconsistent."""
    metadata = extract_audio_metadata(AUDIO_DIR / CHAINED_OGG)

    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.INCONSISTENT,))


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
@pytest.mark.parametrize("fraction", [0.5, 0.9, 0.98])
def test_cut_file_is_never_reported_complete(
    fixture: AudioFixture,
    fraction: float,
    tmp_path: Path,
) -> None:
    """A cut file never reports truncated=false with a duration."""
    metadata = extract_audio_metadata(
        _cut(AUDIO_DIR / fixture.name, tmp_path, fraction)
    )

    reported_complete = (
        metadata.truncated is False and metadata.duration_seconds is not None
    )
    assert_that(reported_complete).is_false()
    if metadata.duration_seconds is not None:
        assert_that(_flags(metadata)).contains(AudioFlag.ESTIMATED)


def _garbage() -> dict[str, bytes]:
    """Build the garbage payloads.

    Returns:
        Name to content.
    """
    return {
        "random": hashlib.shake_256(b"winnow issue 400").digest(8192),
        "zeros": bytes(8192),
        "text": b"The quick brown fox jumps over the lazy dog.\n" * 200,
        "ff": b"\xff" * 8192,
    }


@pytest.mark.parametrize("kind", ["random", "zeros", "text", "ff"])
def test_garbage_under_every_extension(kind: str, tmp_path: Path) -> None:
    """Garbage is unknown under every allow-listed extension."""
    content = _garbage()[kind]
    for suffix in ALLOWED_EXTENSIONS:
        path = tmp_path / f"{kind}{suffix}"
        path.write_bytes(content)

        metadata = extract_audio_metadata(path)

        assert_that(metadata).described_as(path.name).is_equal_to(
            MediaMetadata(audio_flags=(AudioFlag.CORRUPT,)),
        )


def _playlists(target: Path) -> dict[str, bytes]:
    """Build a concat script and HLS playlists that point at ``target``.

    Args:
        target: File the scripts reference.

    Returns:
        Name to content.
    """
    return {
        "concat": f"ffconcat version 1.0\nfile '{target}'\n".encode(),
        "concat_bare": f"file '{target}'\n".encode(),
        "hls": (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\n"
            f"{target}\n#EXT-X-ENDLIST\n"
        ).encode(),
        "hls_file_url": (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\n"
            f"file://{target}\n#EXT-X-ENDLIST\n"
        ).encode(),
    }


@pytest.mark.parametrize("suffix", [".mp3", ".m4a", ".flac", ".wav"])
def test_scripts_never_reach_the_referenced_file(suffix: str, tmp_path: Path) -> None:
    """Concat and HLS under audio names are unknown and open nothing.

    One target is a healthy MP3, so following it would yield facts; the
    other is a FIFO, so opening it would hang until the time-out.
    """
    healthy = _copy(AUDIO_DIR / "mp3.mp3", tmp_path, "target.mp3")
    fifo = tmp_path / "target-fifo.mp3"
    os.mkfifo(fifo)
    for target in (healthy, fifo):
        for name, content in _playlists(target).items():
            path = tmp_path / f"{name}{suffix}"
            path.write_bytes(content)
            started = time.monotonic()

            metadata = extract_audio_metadata(path)

            assert_that(time.monotonic() - started).is_less_than(5)
            assert_that(metadata).described_as(f"{name} -> {target.name}").is_equal_to(
                MediaMetadata(audio_flags=(AudioFlag.CORRUPT,)),
            )


@pytest.mark.parametrize(
    "name",
    ["mp3.mp3", "aac.m4a", "two_tracks.mka", "tta.tta", "cover.m4a", "timecode.mov"],
)
def test_symlink_form_matches_descriptor_form(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private-link fallback gives the same facts as the descriptor."""
    by_descriptor = extract_audio_metadata(AUDIO_DIR / name)
    monkeypatch.setattr(audio_module, "fd_protocol_supported", lambda binary: False)

    by_link = extract_audio_metadata(AUDIO_DIR / name)

    assert_that(by_link).is_equal_to(by_descriptor)


def test_leading_dash_names_are_not_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files named -x.mp4 and -x.mp3 in the working directory are read."""
    _copy(MEDIA_DIR / "sample.mp4", tmp_path, "-x.mp4")
    _copy(AUDIO_DIR / "mp3.mp3", tmp_path, "-x.mp3")
    monkeypatch.chdir(tmp_path)

    video = extract_video_metadata(Path("-x.mp4"))
    audio = extract_audio_metadata(Path("-x.mp3"))

    assert_that((video.width, video.height)).is_equal_to((64, 48))
    assert_that(audio.duration_seconds).is_equal_to(6.0)
