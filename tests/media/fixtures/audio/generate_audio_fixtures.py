"""Regenerate the small audio fixtures used by the ffprobe audio tests.

Run with ``uv run python tests/media/fixtures/audio/generate_audio_fixtures.py``
(optionally followed by an output directory). Every file is produced by the
system ``ffmpeg`` binary from a synthetic source, so the true values in
:data:`FIXTURES` come from the generation parameters below, never from a
reader. ``README.md`` in this directory lists the same values.

Most sources are silence: it keeps lossless files to a few KB while the
container still holds one packet per frame. ``aac.m4a`` keeps its index at the
end of the file and uses a tone, so its audio data outweighs the index.
Durations and packet sizes are chosen so that cutting 2 % off a file removes
more than one packet, which is the smallest loss a packet walk can see.

The only byte handling done here is joining two generated Ogg files into a
chained Ogg file; nothing in this script parses media.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixed argv lists only; never shell=True
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FIXTURE_DIR: Final[Path] = Path(__file__).resolve().parent
MP4_FORMAT: Final[str] = "mov,mp4,m4a,3gp,3g2,mj2"
MKV_FORMAT: Final[str] = "matroska,webm"


@dataclass(frozen=True, slots=True)
class AudioFixture:
    """One generated fixture and the values it was generated with.

    Args:
        name: File name inside the fixture directory.
        inputs: ffmpeg input arguments.
        outputs: ffmpeg output arguments placed before the output path.
        format_name: Expected ffprobe ``format_name``.
        codec: Expected codec name of the primary audio stream.
        sample_rate: Sample rate of the primary audio stream.
        channels: Channel count of the primary audio stream.
        bit_depth: Expected bit depth, or ``None`` when it must be unknown.
        duration: Length of the primary audio source in seconds.
        estimated: ``True`` when the reader must report a measured duration
            flagged as estimated rather than a declared one.
        audio_streams: Number of audio streams in the file.
    """

    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    format_name: str
    codec: str
    sample_rate: int
    channels: int
    bit_depth: int | None
    duration: float
    estimated: bool = False
    audio_streams: int = 1


def _silence(*, rate: int, seconds: float, channels: int = 1) -> tuple[str, ...]:
    """Return lavfi input arguments for silence.

    Args:
        rate: Sample rate in hertz.
        seconds: Length in seconds.
        channels: Channel count (1 or 2).

    Returns:
        ffmpeg input arguments.
    """
    layout = "stereo" if channels == 2 else "mono"
    return (
        "-f",
        "lavfi",
        "-t",
        str(seconds),
        "-i",
        f"anullsrc=channel_layout={layout}:sample_rate={rate}",
    )


def _tone(*, rate: int, seconds: float) -> tuple[str, ...]:
    """Return lavfi input arguments for a mono 440 Hz tone.

    Args:
        rate: Sample rate in hertz.
        seconds: Length in seconds.

    Returns:
        ffmpeg input arguments.
    """
    return (
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
    )


def _sweep(*, rate: int, seconds: float) -> tuple[str, ...]:
    """Return lavfi input arguments for a mono frequency sweep.

    A sweep changes from frame to frame, so a VBR encoder varies its frame
    sizes.

    Args:
        rate: Sample rate in hertz.
        seconds: Length in seconds.

    Returns:
        ffmpeg input arguments.
    """
    return (
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc=0.5*sin(2*PI*(200+2000*t)*t):s={rate}:d={seconds}",
    )


_TINY_VIDEO: Final[tuple[str, ...]] = (
    "-f",
    "lavfi",
    "-i",
    "color=c=black:size=16x16:rate=5:duration=2",
)
_MP3_LOW: Final[tuple[str, ...]] = ("-c:a", "libmp3lame", "-b:a", "8k")
_FASTSTART: Final[tuple[str, ...]] = ("-movflags", "+faststart")

FIXTURES: Final[tuple[AudioFixture, ...]] = (
    # One healthy file per allow-listed demuxer.
    AudioFixture(
        name="mp3.mp3",
        inputs=_silence(rate=24000, seconds=6),
        outputs=_MP3_LOW,
        format_name="mp3",
        codec="mp3",
        sample_rate=24000,
        channels=1,
        bit_depth=None,
        duration=6.0,
    ),
    AudioFixture(
        name="adts.aac",
        inputs=_silence(rate=48000, seconds=2),
        outputs=("-c:a", "aac", "-b:a", "24k", "-f", "adts"),
        format_name="aac",
        codec="aac",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        estimated=True,
    ),
    AudioFixture(
        name="ac3.ac3",
        inputs=_silence(rate=48000, seconds=1, channels=2),
        outputs=("-c:a", "ac3", "-b:a", "64k"),
        format_name="ac3",
        codec="ac3",
        sample_rate=48000,
        channels=2,
        bit_depth=None,
        duration=1.0,
        estimated=True,
    ),
    AudioFixture(
        name="eac3.eac3",
        inputs=_silence(rate=48000, seconds=1, channels=2),
        outputs=("-c:a", "eac3", "-b:a", "64k"),
        format_name="eac3",
        codec="eac3",
        sample_rate=48000,
        channels=2,
        bit_depth=None,
        duration=1.0,
        estimated=True,
    ),
    AudioFixture(
        name="flac.flac",
        inputs=_silence(rate=8000, seconds=2),
        outputs=("-c:a", "flac", "-sample_fmt", "s16", "-frame_size", "160"),
        format_name="flac",
        codec="flac",
        sample_rate=8000,
        channels=1,
        bit_depth=16,
        duration=2.0,
    ),
    AudioFixture(
        name="wavpack.wv",
        inputs=_silence(rate=8000, seconds=2),
        outputs=(
            "-c:a",
            "wavpack",
            "-sample_fmt",
            "s16p",
            "-frame_size",
            "160",
        ),
        format_name="wv",
        codec="wavpack",
        sample_rate=8000,
        channels=1,
        bit_depth=16,
        duration=2.0,
    ),
    AudioFixture(
        name="tta.tta",
        inputs=_silence(rate=8000, seconds=2),
        outputs=("-c:a", "tta", "-sample_fmt", "s16"),
        format_name="tta",
        codec="tta",
        sample_rate=8000,
        channels=1,
        bit_depth=16,
        duration=2.0,
    ),
    AudioFixture(
        name="wav.wav",
        inputs=_silence(rate=8000, seconds=1),
        outputs=("-c:a", "pcm_u8"),
        format_name="wav",
        codec="pcm_u8",
        sample_rate=8000,
        channels=1,
        bit_depth=8,
        duration=1.0,
    ),
    AudioFixture(
        name="aiff.aiff",
        inputs=_silence(rate=8000, seconds=2),
        outputs=("-c:a", "adpcm_ima_qt"),
        format_name="aiff",
        codec="adpcm_ima_qt",
        sample_rate=8000,
        channels=1,
        bit_depth=None,
        duration=2.0,
    ),
    AudioFixture(
        name="aac.m4a",
        inputs=_tone(rate=48000, seconds=2),
        outputs=("-c:a", "aac", "-b:a", "24k"),
        format_name=MP4_FORMAT,
        codec="aac",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
    ),
    AudioFixture(
        name="opus.ogg",
        inputs=_silence(rate=48000, seconds=2),
        outputs=("-c:a", "libopus", "-b:a", "16k"),
        format_name="ogg",
        codec="opus",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        estimated=True,
    ),
    AudioFixture(
        name="wma.wma",
        inputs=_silence(rate=22050, seconds=3),
        outputs=("-c:a", "wmav2", "-b:a", "24k", "-packet_size", "200", "-f", "asf"),
        format_name="asf",
        codec="wmav2",
        sample_rate=22050,
        channels=1,
        bit_depth=None,
        duration=3.0,
    ),
    AudioFixture(
        name="opus.mka",
        inputs=_silence(rate=48000, seconds=2),
        outputs=("-c:a", "libopus", "-b:a", "16k"),
        format_name=MKV_FORMAT,
        codec="opus",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
    ),
    AudioFixture(
        name="pcm.avi",
        inputs=_silence(rate=8000, seconds=1),
        outputs=("-c:a", "pcm_u8"),
        format_name="avi",
        codec="pcm_u8",
        sample_rate=8000,
        channels=1,
        bit_depth=8,
        duration=1.0,
        estimated=True,
    ),
    AudioFixture(
        name="mp2.m2ts",
        inputs=_silence(rate=48000, seconds=2),
        outputs=("-c:a", "mp2", "-b:a", "32k", "-f", "mpegts"),
        format_name="mpegts",
        codec="mp2",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        estimated=True,
    ),
    AudioFixture(
        name="mp2.mpg",
        inputs=_silence(rate=48000, seconds=2),
        outputs=("-c:a", "mp2", "-b:a", "32k", "-f", "mpeg"),
        format_name="mpeg",
        codec="mp2",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        estimated=True,
    ),
    AudioFixture(
        name="mp3.flv",
        inputs=_silence(rate=22050, seconds=2),
        outputs=("-c:a", "libmp3lame", "-b:a", "32k"),
        format_name="flv",
        codec="mp3",
        sample_rate=22050,
        channels=1,
        bit_depth=None,
        duration=2.0,
        estimated=True,
    ),
    # Codecs that the previous Python reader mislabelled.
    AudioFixture(
        name="mp2_named.mp3",
        inputs=_silence(rate=48000, seconds=2),
        outputs=("-c:a", "mp2", "-b:a", "32k", "-f", "mp2"),
        format_name="mp3",
        codec="mp2",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        estimated=True,
    ),
    AudioFixture(
        name="alac.m4a",
        inputs=_silence(rate=8000, seconds=2),
        outputs=("-c:a", "alac", "-sample_fmt", "s16p", *_FASTSTART),
        format_name=MP4_FORMAT,
        codec="alac",
        sample_rate=8000,
        channels=1,
        bit_depth=16,
        duration=2.0,
    ),
    AudioFixture(
        name="adpcm_ms.wav",
        inputs=_silence(rate=8000, seconds=2),
        outputs=("-c:a", "adpcm_ms"),
        format_name="wav",
        codec="adpcm_ms",
        sample_rate=8000,
        channels=1,
        bit_depth=None,
        duration=2.0,
    ),
    AudioFixture(
        name="ac3_mono.ac3",
        inputs=_silence(rate=48000, seconds=1),
        outputs=("-c:a", "ac3", "-b:a", "48k"),
        format_name="ac3",
        codec="ac3",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=1.0,
        estimated=True,
    ),
    AudioFixture(
        name="flac24.flac",
        inputs=_silence(rate=8000, seconds=2),
        outputs=(
            "-c:a",
            "flac",
            "-sample_fmt",
            "s32",
            "-bits_per_raw_sample",
            "24",
            "-frame_size",
            "160",
        ),
        format_name="flac",
        codec="flac",
        sample_rate=8000,
        channels=1,
        bit_depth=24,
        duration=2.0,
    ),
    # Stream selection (R3): the second track is the default one. Neither
    # track declares its own duration and the file has two streams, so the
    # duration is measured (R5).
    AudioFixture(
        name="two_tracks.mka",
        inputs=(
            *_silence(rate=8000, seconds=2),
            *_silence(rate=16000, seconds=2, channels=2),
        ),
        outputs=(
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:a",
            "flac",
            "-sample_fmt",
            "s16",
            "-frame_size",
            "160",
            "-disposition:a:0",
            "0",
            "-disposition:a:1",
            "default",
        ),
        format_name=MKV_FORMAT,
        codec="flac",
        sample_rate=16000,
        channels=2,
        bit_depth=16,
        duration=2.0,
        estimated=True,
        audio_streams=2,
    ),
    # Cover art is an attached picture, never a video stream (R3).
    AudioFixture(
        name="cover.mp3",
        inputs=(
            *_silence(rate=24000, seconds=6),
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=8x8:duration=1",
        ),
        outputs=(
            "-map",
            "0:a",
            "-map",
            "1:v",
            *_MP3_LOW,
            "-c:v",
            "png",
            "-disposition:v",
            "attached_pic",
        ),
        format_name="mp3",
        codec="mp3",
        sample_rate=24000,
        channels=1,
        bit_depth=None,
        duration=6.0,
    ),
    # Audio that starts one second after the video (R5).
    AudioFixture(
        name="delayed.mkv",
        inputs=(
            *_TINY_VIDEO,
            "-itsoffset",
            "1",
            *_silence(rate=48000, seconds=1),
        ),
        outputs=(
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "libopus",
            "-b:a",
            "16k",
        ),
        format_name=MKV_FORMAT,
        codec="opus",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=1.0,
    ),
    AudioFixture(
        name="delayed.mp4",
        inputs=(
            *_TINY_VIDEO,
            "-itsoffset",
            "1",
            *_silence(rate=48000, seconds=1),
        ),
        outputs=(
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            *_FASTSTART,
        ),
        format_name=MP4_FORMAT,
        codec="aac",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=1.0,
    ),
    # A VBR MP3 without a Xing header has no declared duration.
    AudioFixture(
        name="vbr_noxing.mp3",
        inputs=_sweep(rate=44100, seconds=2),
        outputs=("-c:a", "libmp3lame", "-q:a", "9", "-write_xing", "0"),
        format_name="mp3",
        codec="mp3",
        sample_rate=44100,
        channels=1,
        bit_depth=None,
        duration=2.0,
        estimated=True,
    ),
)
"""Fixtures written by ffmpeg directly."""

CHAINED_OGG: Final[str] = "chained.ogg"
"""Two one-second Ogg Opus links with different serial numbers, back to back."""

_CHAIN_SERIALS: Final[tuple[str, ...]] = ("100", "200")


def _ffmpeg(*, inputs: tuple[str, ...], outputs: tuple[str, ...], dest: Path) -> None:
    """Run ffmpeg with a fixed argument vector.

    Args:
        inputs: Input arguments.
        outputs: Output arguments.
        dest: Output file.

    Raises:
        SystemExit: When ffmpeg is not installed.
    """
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SystemExit("ffmpeg is required to generate the audio fixtures")
    argv = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *inputs,
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags",
        "+bitexact",
        *outputs,
        str(dest),
    ]
    subprocess.run(argv, check=True)  # nosec B603 - fixed argv, resolved binary


def generate(directory: Path) -> list[Path]:
    """Write every audio fixture into ``directory``.

    Args:
        directory: Existing output directory.

    Returns:
        Paths written.
    """
    written: list[Path] = []
    for fixture in FIXTURES:
        dest = directory / fixture.name
        _ffmpeg(inputs=fixture.inputs, outputs=fixture.outputs, dest=dest)
        written.append(dest)

    with tempfile.TemporaryDirectory() as scratch:
        parts: list[Path] = []
        for serial in _CHAIN_SERIALS:
            part = Path(scratch) / f"link{serial}.ogg"
            _ffmpeg(
                inputs=_silence(rate=48000, seconds=1),
                outputs=("-c:a", "libopus", "-b:a", "16k", "-serial_offset", serial),
                dest=part,
            )
            parts.append(part)
        chained = directory / CHAINED_OGG
        chained.write_bytes(b"".join(part.read_bytes() for part in parts))
        written.append(chained)
    return written


def main() -> None:
    """Generate the fixtures into the given directory or this one."""
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    total = 0
    for path in generate(directory):
        size = path.stat().st_size
        total += size
        print(f"{path.name}: {size} bytes")
    print(f"total: {total} bytes")


if __name__ == "__main__":
    main()
