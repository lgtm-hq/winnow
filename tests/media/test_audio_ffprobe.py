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
    EMPTY_CONTAINER_FIXTURES,
    EMPTY_FIXTURES,
    FIXTURES,
    FRAGMENT_CUTS,
    FRAME_CUTS,
    FRAME_SOURCES,
    HE_AAC_EXCESS,
    HE_AAC_FIXTURE,
    INCONSISTENT_FIXTURES,
    UNSUPPORTED_FIXTURES,
    AudioFixture,
    FragmentCut,
    FrameCut,
)
from winnow.media import audio as audio_module
from winnow.media._ffprobe import (
    FFMPEG,
    ffmpeg_available,
    ffprobe_available,
    ffprobe_version,
    find_binary,
    parse_version_major,
    run_command,
)
from winnow.media.audio import BENIGN_STDERR_BY_MAJOR, extract_audio_metadata
from winnow.media.video import extract_video_metadata
from winnow.models.media import AudioFlag, MediaMetadata

pytestmark = pytest.mark.skipif(not ffprobe_available(), reason="ffprobe not installed")

AUDIO_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures" / "audio"
MEDIA_DIR: Final[Path] = Path(__file__).resolve().parent / "fixtures"
HEALTHY: Final[tuple[AudioFixture, ...]] = (*FIXTURES, *FRAME_SOURCES)
BY_NAME: Final[dict[str, AudioFixture]] = {fixture.name: fixture for fixture in HEALTHY}
INCONSISTENT_BY_NAME: Final[dict[str, AudioFixture]] = {
    fixture.name: fixture for fixture in INCONSISTENT_FIXTURES
}


@dataclass(frozen=True, slots=True)
class Pins:
    """Build-dependent values for one FFmpeg major.

    Args:
        benign: Benign stderr lines.
        stream_bitrate: Fixtures with a declared duration whose primary
            stream has a ``bit_rate`` in this build.
        cut_outcomes: For each fixture in ``FIXTURES``, the outcome of the
            file cut to 50, 90 and 98 % (see :func:`_outcome`).
    """

    benign: frozenset[str]
    stream_bitrate: frozenset[str]
    cut_outcomes: dict[str, tuple[str, str, str]]


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
                "cover.m4a",
                "timecode.mov",
                "frames.mp3",
                "frames.m4a",
                "latin1_tag.wav",
                "latin1_tag.aiff",
            },
        ),
        cut_outcomes={
            "mp3.mp3": ("unrecognized_output", "unrecognized_output", "truncated"),
            "adts.aac": ("estimated", "estimated", "estimated"),
            "ac3.ac3": ("estimated", "estimated", "estimated"),
            "eac3.eac3": ("estimated", "estimated", "estimated"),
            "flac.flac": ("unsupported", "truncated", "truncated"),
            "wavpack.wv": ("truncated", "truncated", "truncated"),
            "tta.tta": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "wav.wav": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "aiff.aiff": ("truncated", "truncated", "truncated"),
            "aac.m4a": ("unsupported", "unrecognized_output", "unsupported"),
            "opus.ogg": ("unsupported", "estimated", "estimated"),
            "wma.wma": ("estimated", "estimated", "truncated"),
            "opus.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "pcm.avi": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "mp2.m2ts": ("unrecognized_output", "estimated", "unrecognized_output"),
            "mp2.mpg": ("estimated", "unrecognized_output", "unrecognized_output"),
            "mp3.flv": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "mp2_named.mp3": ("estimated", "estimated", "estimated"),
            "alac.m4a": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "adpcm_ms.wav": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "ac3_mono.ac3": ("estimated", "estimated", "estimated"),
            "flac24.flac": ("unsupported", "truncated", "truncated"),
            "two_tracks.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "cover.mp3": ("unrecognized_output", "unrecognized_output", "truncated"),
            "delayed.mkv": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "delayed.mp4": (
                "unsupported",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "delayed.avi": ("unsupported", "estimated", "estimated"),
            "video.wmv": ("estimated", "estimated", "estimated"),
            "vbr_noxing.mp3": ("estimated", "estimated", "estimated"),
            "cover.m4a": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "timecode.mov": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "offset.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "offset_delayed.mkv": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "delayed_audio.avi": ("unsupported", "estimated", "estimated"),
            "delayed_audio.wma": ("estimated", "estimated", "estimated"),
            "delayed_pcm.mkv": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "late_pcm.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "latin1_tag.wav": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "latin1_tag.aiff": ("truncated", "truncated", "truncated"),
            "latin1_tag.avi": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "delay_moov.mp4": ("unsupported", "unrecognized_output", "estimated"),
            "vorbis.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "wma.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "mp2.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "ac3.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "eac3.mka": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "mp3_frag.mp4": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "aac_frag.mp4": ("unsupported", "unrecognized_output", "estimated"),
            "vorbis_frag.mp4": (
                "unsupported",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "opus_frag.mp4": (
                "unrecognized_output",
                "unrecognized_output",
                "unrecognized_output",
            ),
            "aac_fragments.mp4": ("unrecognized_output", "truncated", "estimated"),
            "alac_fragments.mp4": (
                "unrecognized_output",
                "unrecognized_output",
                "estimated",
            ),
            "alac_indexed.mp4": (
                "unrecognized_output",
                "unrecognized_output",
                "estimated",
            ),
            "alac_bframes.mp4": ("unsupported", "unrecognized_output", "estimated"),
        },
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
    assert_that(metadata.truncated).is_same_as(False)
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


@pytest.mark.parametrize("name", ["delayed.mkv", "delayed.avi", "video.wmv"])
def test_audio_beside_video_is_estimated_never_truncated(name: str) -> None:
    """A5, A9b and A8: the audio is measured within the tolerance, never truncated.

    Delayed Matroska audio has no declared duration once the ``DURATION`` tag
    is unused; AVI never declares one, its audio duration field includes the
    delay (A9b); in ASF the field is the video's length, so it is not used
    beside another stream (A8).
    """
    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(_duration(metadata)).is_close_to(
        BY_NAME[name].duration,
        DURATION_TOLERANCE,
    )


@pytest.mark.parametrize("name", ["offset.mka", "offset_delayed.mkv"])
def test_start_offset_is_estimated_not_truncated(name: str) -> None:
    """A9: a Matroska file that starts after zero is measured, never truncated."""
    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(_duration(metadata)).is_close_to(
        BY_NAME[name].duration,
        DURATION_TOLERANCE,
    )


@pytest.mark.parametrize(
    "name",
    [
        "delayed.mp4",
        "delayed_audio.avi",
        "delayed_audio.wma",
        "delayed_pcm.mkv",
        "late_pcm.mka",
    ],
)
def test_late_start_is_estimated_never_truncated(name: str) -> None:
    """A9 and A9b: audio that starts late is measured, never truncated.

    The audio's duration field there is the end of the file's timeline,
    or in AVI includes the delay while the packets start at zero.
    """
    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(_duration(metadata)).is_close_to(
        BY_NAME[name].duration,
        DURATION_TOLERANCE,
    )


def test_delayed_fragmented_mp4_is_inconsistent() -> None:
    """A10b: a first packet stretched over a 0.1 s delay leaves no duration.

    The stream facts stay known; only the duration is unknown.
    """
    fixture = INCONSISTENT_BY_NAME["delayed_frag.mp4"]
    metadata = extract_audio_metadata(AUDIO_DIR / fixture.name)

    assert_that(metadata.container).is_equal_to(fixture.format_name)
    assert_that(metadata.codec).is_equal_to(fixture.codec)
    assert_that(metadata.sample_rate).is_equal_to(fixture.sample_rate)
    assert_that(metadata.channels).is_equal_to(fixture.channels)
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.bitrate).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.INCONSISTENT,))


def test_aac_surround_flv_makes_every_fact_unknown() -> None:
    """A11: AAC 5.1 in FLV has an untimed packet, so no audio fact is kept."""
    metadata = extract_audio_metadata(
        AUDIO_DIR / INCONSISTENT_BY_NAME["aac51.flv"].name,
    )

    assert_that(metadata).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,)),
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
    assert_that(metadata.truncated).is_same_as(False)
    assert_that(_flags(metadata)).is_empty()
    assert_that(_duration(metadata)).is_close_to(fixture.duration, DURATION_TOLERANCE)


@pytest.mark.parametrize("cut", FRAME_CUTS, ids=lambda cut: cut.name)
def test_last_frames_removed(cut: FrameCut) -> None:
    """Removing the last frames: each pinned cut is caught or missed as pinned.

    In the faststart files two and three frames are caught and one is not.
    The miss is the documented limit: the encoder's end padding is longer
    than the frame removed, so the audio left still reaches the declared
    length within max(5 ms, two time-base units) (A5). In fragmented MP3 and
    AAC, cuts of one, three and ten packets are all ``truncated`` (A14a).
    """
    source = BY_NAME[cut.source]

    metadata = extract_audio_metadata(AUDIO_DIR / cut.name)

    assert_that(metadata.codec).is_equal_to(source.codec)
    assert_that(_flags(metadata)).is_empty()
    if cut.detected:
        assert_that(metadata.truncated).is_same_as(True)
        assert_that(metadata.duration_seconds).is_none()
    else:
        assert_that(metadata.truncated).is_same_as(False)
        assert_that(_duration(metadata)).is_close_to(
            source.duration,
            DURATION_TOLERANCE,
        )


@pytest.mark.parametrize("cut", FRAGMENT_CUTS, ids=lambda cut: cut.name)
def test_fragment_boundary_cut_is_never_complete(cut: FragmentCut) -> None:
    """A15a: a fragmented MP4 cut on a fragment boundary is never complete.

    Without a fragment index the declared duration shrinks with the
    packets, so the three fragments left (1.536 s) are measured, lossless
    included. With a ``sidx`` index the declared 2 s stands and the cut is
    ``truncated``.
    """
    source = BY_NAME[cut.source]

    metadata = extract_audio_metadata(AUDIO_DIR / cut.name)

    assert_that(metadata.codec).is_equal_to(source.codec)
    assert_that(metadata.truncated).is_equal_to(cut.truncated)
    if cut.truncated is True:
        assert_that(_flags(metadata)).is_empty()
        assert_that(metadata.duration_seconds).is_none()
        return
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(_duration(metadata)).is_close_to(
        source.duration * 3 / 4,
        DURATION_TOLERANCE,
    )


def test_he_aac_is_estimated() -> None:
    """A15b: HE-AAC in MP4 declares more than a codec frame past the audio.

    ffprobe names the profile, so the duration is measured, not checked. The
    measured span counts the priming, so it is held to the stated range past
    the true length.
    """
    fixture = HE_AAC_FIXTURE

    metadata = extract_audio_metadata(AUDIO_DIR / fixture.name)

    assert_that(metadata.container).is_equal_to(fixture.format_name)
    assert_that(metadata.codec).is_equal_to(fixture.codec)
    assert_that(metadata.sample_rate).is_equal_to(fixture.sample_rate)
    assert_that(metadata.channels).is_none()  # mono HE-AAC also reads as 2
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.bitrate).is_none()
    low, high = HE_AAC_EXCESS
    assert_that(_duration(metadata) - fixture.duration).is_between(low, high)


def test_two_tracks_and_cover_art() -> None:
    """The default track is primary; cover art is not a stream (R3)."""
    tracks = extract_audio_metadata(AUDIO_DIR / "two_tracks.mka")
    cover = extract_audio_metadata(AUDIO_DIR / "cover.mp3")

    assert_that((tracks.sample_rate, tracks.channels)).is_equal_to((16000, 2))
    assert_that(tracks.audio_stream_count).is_equal_to(2)
    assert_that(cover.audio_stream_count).is_equal_to(1)
    assert_that(cover.truncated).is_same_as(False)
    assert_that(_flags(cover)).is_empty()


def test_chained_ogg_is_inconsistent() -> None:
    """Chained Ogg has duration unknown and is flagged inconsistent."""
    metadata = extract_audio_metadata(AUDIO_DIR / CHAINED_OGG)

    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.INCONSISTENT,))


CUT_FRACTIONS: Final[tuple[float, ...]] = (0.5, 0.9, 0.98)


def _outcome(metadata: MediaMetadata) -> str:
    """Name the reader's verdict on a cut file.

    Args:
        metadata: Reader output.

    Returns:
        ``truncated``, ``complete`` (``truncated=false``), the first flag's
        value, or ``none``.
    """
    if metadata.truncated is True:
        return "truncated"
    if metadata.truncated is False:
        return "complete"
    flags = _flags(metadata)
    return flags[0].value if flags else "none"


def test_cut_outcomes_cover_every_fixture(pins: Pins) -> None:
    """Every cut fixture has a pinned outcome, and none is ``complete``."""
    assert_that(set(pins.cut_outcomes)).is_equal_to({f.name for f in FIXTURES})
    for outcomes in pins.cut_outcomes.values():
        assert_that(outcomes).does_not_contain("complete", "corrupt")


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
@pytest.mark.parametrize("fraction", CUT_FRACTIONS)
def test_cut_file_is_never_reported_complete(
    fixture: AudioFixture,
    fraction: float,
    pins: Pins,
    tmp_path: Path,
) -> None:
    """A cut file never reports truncated=false with a duration.

    The outcome of each cut is pinned per fixture, so a rule change that
    moves a cut from ``truncated`` to another verdict is seen.
    """
    metadata = extract_audio_metadata(
        _cut(AUDIO_DIR / fixture.name, tmp_path, fraction)
    )

    expected = pins.cut_outcomes[fixture.name][CUT_FRACTIONS.index(fraction)]
    assert_that(_outcome(metadata)).is_equal_to(expected)

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
    """Garbage is unknown and unsupported, never corrupt, under every extension."""
    content = _garbage()[kind]
    for suffix in ALLOWED_EXTENSIONS:
        path = tmp_path / f"{kind}{suffix}"
        path.write_bytes(content)

        metadata = extract_audio_metadata(path)

        assert_that(metadata).described_as(path.name).is_equal_to(
            MediaMetadata(audio_flags=(AudioFlag.UNSUPPORTED,)),
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
    """Concat and HLS under audio names are unsupported and open nothing.

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
                MediaMetadata(audio_flags=(AudioFlag.UNSUPPORTED,)),
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


@pytest.mark.parametrize(
    "fixture",
    UNSUPPORTED_FIXTURES,
    ids=lambda fixture: fixture.name,
)
def test_formats_outside_the_allow_list_are_unsupported(fixture: AudioFixture) -> None:
    """A12b: healthy AU and Core Audio Format files are unsupported."""
    metadata = extract_audio_metadata(AUDIO_DIR / fixture.name)

    assert_that(metadata).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.UNSUPPORTED,)),
    )


@pytest.mark.parametrize("fixture", EMPTY_FIXTURES, ids=lambda fixture: fixture.name)
def test_empty_files_are_not_damaged(fixture: AudioFixture) -> None:
    """A12d: an empty FLAC or AIFF keeps its facts and is never truncated."""
    metadata = extract_audio_metadata(AUDIO_DIR / fixture.name)

    assert_that(metadata.container).is_equal_to(fixture.format_name)
    assert_that(metadata.codec).is_equal_to(fixture.codec)
    assert_that(metadata.sample_rate).is_equal_to(fixture.sample_rate)
    assert_that(metadata.channels).is_equal_to(fixture.channels)
    assert_that(metadata.bit_depth).is_equal_to(fixture.bit_depth)
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.bitrate).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.INCONSISTENT,))


@pytest.mark.parametrize(
    "fixture",
    EMPTY_CONTAINER_FIXTURES,
    ids=lambda fixture: fixture.name,
)
def test_empty_mp4_and_mov_say_why(fixture: AudioFixture) -> None:
    """A13c, A14b: an M4A or MOV with no stream is all unknown, ``no_audio_stream``."""
    metadata = extract_audio_metadata(AUDIO_DIR / fixture.name)

    assert_that(metadata).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.NO_AUDIO_STREAM,)),
    )


ALL_FIXTURE_NAMES: Final[tuple[str, ...]] = (
    *(
        fixture.name
        for fixture in (
            *FIXTURES,
            *INCONSISTENT_FIXTURES,
            *UNSUPPORTED_FIXTURES,
            *EMPTY_FIXTURES,
            *EMPTY_CONTAINER_FIXTURES,
            *FRAME_SOURCES,
            HE_AAC_FIXTURE,
        )
    ),
    CHAINED_OGG,
    *(cut.name for cut in FRAME_CUTS),
    *(cut.name for cut in FRAGMENT_CUTS),
)
"""Every generated audio fixture."""

AUDIO_FACT_FIELDS: Final[tuple[str, ...]] = (
    "container",
    "codec",
    "sample_rate",
    "channels",
    "bit_depth",
    "bitrate",
    "duration_seconds",
    "audio_stream_count",
    "probe_score",
    "truncated",
)
"""Fields of :class:`MediaMetadata` the audio reader reports as facts."""


def test_every_all_unknown_result_says_why(tmp_path: Path) -> None:
    """A13c: any result with every fact unknown carries at least one flag.

    Checked on every fixture and on each one cut to 50, 90 and 98 %. The
    set of all-unknown results is pinned so the check is never vacuous.
    """
    all_unknown: set[str] = set()
    for name in ALL_FIXTURE_NAMES:
        sources = [AUDIO_DIR / name]
        sources += [
            _cut(AUDIO_DIR / name, tmp_path, fraction) for fraction in CUT_FRACTIONS
        ]
        for source in sources:
            metadata = extract_audio_metadata(source)
            if all(getattr(metadata, field) is None for field in AUDIO_FACT_FIELDS):
                all_unknown.add(source.name)
                assert_that(_flags(metadata)).described_as(source.name).is_not_empty()

    assert_that(all_unknown).contains(
        "au.au",
        "core_audio.bin",
        "aac51.flv",
        "empty.m4a",
        "empty.mov",
        "cut-0.5-empty.m4a",
    )


@pytest.mark.parametrize(
    "name", ["latin1_tag.wav", "latin1_tag.aiff", "latin1_tag.avi"]
)
def test_non_utf8_tags_do_not_hide_the_facts(name: str) -> None:
    """A12a: a Latin-1 title tag is never read, so the facts stand."""
    fixture = BY_NAME[name]

    metadata = extract_audio_metadata(AUDIO_DIR / name)

    assert_that((metadata.container, metadata.codec)).is_equal_to(
        (fixture.format_name, fixture.codec),
    )
    assert_that(_duration(metadata)).is_close_to(fixture.duration, DURATION_TOLERANCE)
    assert_that(_flags(metadata)).does_not_contain(
        AudioFlag.CORRUPT,
        AudioFlag.UNRECOGNIZED_OUTPUT,
    )


_PIPED_WAV: Final[MediaMetadata] = MediaMetadata(
    container="wav",
    codec="pcm_s16le",
    sample_rate=8000,
    channels=1,
    bit_depth=16,
    audio_stream_count=1,
    probe_score=99,
)
"""Stream facts of the piped 8 kHz mono WAV files."""


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
@pytest.mark.parametrize(
    ("muxer", "codec", "seconds", "expected"),
    [
        pytest.param(
            "wav",
            "pcm_s16le",
            "1",
            MediaMetadata(audio_flags=(AudioFlag.UNRECOGNIZED_OUTPUT,)),
            id="short-wav",
        ),
        pytest.param(
            "wav",
            "pcm_s16le",
            "10",
            _PIPED_WAV.model_copy(
                update={"audio_flags": (AudioFlag.UNRECOGNIZED_OUTPUT,)},
            ),
            id="long-wav",
        ),
        pytest.param(
            "wav",
            "pcm_s16le",
            "2.048",
            _PIPED_WAV.model_copy(
                update={
                    "duration_seconds": 2.048,
                    "audio_flags": (AudioFlag.ESTIMATED,),
                },
            ),
            id="whole-packets-wav",
        ),
        pytest.param(
            "aiff",
            "pcm_s16be",
            "1",
            MediaMetadata(audio_flags=(AudioFlag.UNSUPPORTED,)),
            id="aiff",
        ),
    ],
)
def test_written_to_a_pipe_is_never_damaged(
    muxer: str,
    codec: str,
    seconds: str,
    expected: MediaMetadata,
    tmp_path: Path,
) -> None:
    """A12b and A13b: WAV and AIFF written to a pipe make no damage claim.

    ``-seekable 0`` makes ffmpeg write exactly what it writes to a pipe: it
    cannot seek back to fill in the sizes. The AIFF header does not parse
    at all (A12b). The WAV header parses; when its data does not end on a
    4096-byte packet, ffprobe prints ``Packet corrupt`` for the short last
    packet (A1). In a short file the header call reads that far and every
    fact is unknown; in a long one only the walk does, so the stream facts
    stand and the duration is unknown. Data that is a whole number of
    packets (2.048 s here) gives no such line and is ``estimated`` with the
    right duration.
    """
    binary = find_binary(FFMPEG)
    if binary is None:
        pytest.fail("ffmpeg not found")
    target = tmp_path / f"piped.{muxer}"
    completed = run_command(
        argv=[
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            seconds,
            "-c:a",
            codec,
            "-seekable",
            "0",
            "-f",
            muxer,
            f"file:{target}",
        ],
        timeout=60,
        operation="pipe fixture",
        path=target,
    )
    assert_that(completed.returncode).is_equal_to(0)

    assert_that(extract_audio_metadata(target)).is_equal_to(expected)
