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

ffmpeg and ffprobe run through winnow's own runner
(:func:`winnow.media._ffprobe.run_command`). The only byte handling done
here is joining two generated Ogg files into a chained Ogg file and cutting
the last frames off two files at offsets ffprobe reports (:data:`FRAME_CUTS`);
nothing in this script parses media. Every output is byte for byte the same
on each run with the same FFmpeg build.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from winnow.media._ffprobe import FFMPEG, FFPROBE, find_binary, run_command

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
        bitrate: Nominal bitrate the parameters fix: the encoder's constant
            target, or the PCM or ADPCM data rate (sample rate times bits per
            sample; IMA QT ADPCM packs 64 samples into 34-byte blocks);
            ``None`` when they fix none (lossless, VBR, or silence that an
            AAC encoder codes far below its target).
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
    bitrate: int | None = None


def _silence(*, rate: int, seconds: float, channels: int = 1) -> tuple[str, ...]:
    """Return lavfi input arguments for silence.

    Args:
        rate: Sample rate in hertz.
        seconds: Length in seconds.
        channels: Channel count (1, 2, or 6 for 5.1).

    Returns:
        ffmpeg input arguments.
    """
    layout = {1: "mono", 2: "stereo", 6: "5.1"}[channels]
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
        bitrate=8000,
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
        bitrate=64000,
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
        bitrate=34000,
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
        bitrate=24000,
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
        bitrate=24000,
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
        outputs=(
            "-c:a",
            "mp2",
            "-b:a",
            "32k",
            "-f",
            "mpegts",
            "-mpegts_m2ts_mode",
            "0",
        ),
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
        bitrate=32000,
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
        bitrate=8000,
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
        estimated=True,
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
        estimated=True,
    ),
    # A8: in AVI and ASF with video, the audio duration field is not about
    # the audio alone, so nothing is declared.
    AudioFixture(
        name="delayed.avi",
        inputs=(
            *_TINY_VIDEO,
            "-itsoffset",
            "1",
            *_silence(rate=24000, seconds=1),
        ),
        outputs=(
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "mpeg4",
            *_MP3_LOW,
        ),
        format_name="avi",
        codec="mp3",
        sample_rate=24000,
        channels=1,
        bit_depth=None,
        duration=1.0,
        estimated=True,
    ),
    AudioFixture(
        name="video.wmv",
        inputs=(*_TINY_VIDEO, *_silence(rate=22050, seconds=1)),
        outputs=(
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "wmv2",
            "-c:a",
            "wmav2",
            "-b:a",
            "24k",
            "-f",
            "asf",
        ),
        format_name="asf",
        codec="wmav2",
        sample_rate=22050,
        channels=1,
        bit_depth=None,
        duration=1.0,
        estimated=True,
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
    # Cover art in MP4 (A3): ffprobe prints "stream 0, timescale not set" for
    # the covr image. The index is at the start so a cut removes audio.
    AudioFixture(
        name="cover.m4a",
        inputs=(
            *_tone(rate=48000, seconds=2),
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
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            "-c:v",
            "png",
            "-disposition:v",
            "attached_pic",
            *_FASTSTART,
        ),
        format_name=MP4_FORMAT,
        codec="aac",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        bitrate=24000,
    ),
    # A timecode (tmcd) data track (A2): ffprobe prints "Unsupported codec
    # with id 0 for input stream 2" for it.
    AudioFixture(
        name="timecode.mov",
        inputs=(
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=16x16:rate=25:duration=2",
            *_tone(rate=48000, seconds=2),
        ),
        outputs=(
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            "-timecode",
            "01:00:00:00",
            *_FASTSTART,
        ),
        format_name=MP4_FORMAT,
        codec="aac",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        bitrate=24000,
    ),
    # Audio-only Matroska that starts at 5 s (A4): the DURATION tag and
    # format.duration are end times, so the duration is measured.
    AudioFixture(
        name="offset.mka",
        inputs=_silence(rate=8000, seconds=2),
        outputs=(
            "-c:a",
            "flac",
            "-sample_fmt",
            "s16",
            "-frame_size",
            "160",
            "-output_ts_offset",
            "5",
        ),
        format_name=MKV_FORMAT,
        codec="flac",
        sample_rate=8000,
        channels=1,
        bit_depth=16,
        duration=2.0,
        estimated=True,
    ),
    # Delayed audio in a Matroska file that starts at 0.5 s (A4).
    AudioFixture(
        name="offset_delayed.mkv",
        inputs=(
            *_TINY_VIDEO,
            "-itsoffset",
            "1",
            *_silence(rate=8000, seconds=1),
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
            "flac",
            "-sample_fmt",
            "s16",
            "-frame_size",
            "160",
            "-output_ts_offset",
            "0.5",
        ),
        format_name=MKV_FORMAT,
        codec="flac",
        sample_rate=8000,
        channels=1,
        bit_depth=16,
        duration=1.0,
        estimated=True,
    ),
    # A9 and A9b: audio-only AVI and ASF delayed by 1 s. Their audio duration
    # field includes the delay; AVI packets start at 0, ASF packets at 1 s.
    AudioFixture(
        name="delayed_audio.avi",
        inputs=("-itsoffset", "1", *_silence(rate=24000, seconds=1)),
        outputs=_MP3_LOW,
        format_name="avi",
        codec="mp3",
        sample_rate=24000,
        channels=1,
        bit_depth=None,
        duration=1.0,
        estimated=True,
    ),
    AudioFixture(
        name="delayed_audio.wma",
        inputs=("-itsoffset", "1", *_silence(rate=22050, seconds=1)),
        outputs=("-c:a", "wmav2", "-b:a", "24k", "-f", "asf"),
        format_name="asf",
        codec="wmav2",
        sample_rate=22050,
        channels=1,
        bit_depth=None,
        duration=1.0,
        estimated=True,
    ),
    # A9: PCM Matroska whose audio starts late, beside video by 1 s and alone
    # by 5 ms. The stream duration is the end of the file's timeline.
    AudioFixture(
        name="delayed_pcm.mkv",
        inputs=(
            *_TINY_VIDEO,
            "-itsoffset",
            "1",
            *_silence(rate=8000, seconds=1),
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
            "pcm_u8",
        ),
        format_name=MKV_FORMAT,
        codec="pcm_u8",
        sample_rate=8000,
        channels=1,
        bit_depth=8,
        duration=1.0,
        estimated=True,
    ),
    AudioFixture(
        name="late_pcm.mka",
        inputs=("-itsoffset", "0.005", *_silence(rate=8000, seconds=1)),
        outputs=("-c:a", "pcm_u8"),
        format_name=MKV_FORMAT,
        codec="pcm_u8",
        sample_rate=8000,
        channels=1,
        bit_depth=8,
        duration=1.0,
        estimated=True,
    ),
)
"""Fixtures written by ffmpeg directly, sized so a 2 % cut removes a packet."""


INCONSISTENT_FIXTURES: Final[tuple[AudioFixture, ...]] = (
    # A10b: fragmented MP4 stretches its first AAC packet across a 0.1 s
    # start delay.
    AudioFixture(
        name="delayed_frag.mp4",
        inputs=(
            *_TINY_VIDEO,
            "-itsoffset",
            "0.1",
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
            "-movflags",
            "+frag_keyframe+empty_moov",
        ),
        format_name=MP4_FORMAT,
        codec="aac",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=1.0,
    ),
    # A11: AAC 5.1 in FLV has an untimed configuration packet.
    AudioFixture(
        name="aac51.flv",
        inputs=_silence(rate=44100, seconds=1, channels=6),
        outputs=("-c:a", "aac", "-b:a", "128k"),
        format_name="flv",
        codec="aac",
        sample_rate=44100,
        channels=6,
        bit_depth=None,
        duration=1.0,
    ),
)
"""Healthy files whose evidence does not establish the duration.

The reader must report the duration unknown with ``inconsistent`` and never
``truncated``; for ``aac51.flv`` every audio fact is unknown (A11).
"""


FRAME_SOURCES: Final[tuple[AudioFixture, ...]] = (
    AudioFixture(
        name="frames.mp3",
        inputs=_tone(rate=44100, seconds=2),
        outputs=("-c:a", "libmp3lame", "-b:a", "32k"),
        format_name="mp3",
        codec="mp3",
        sample_rate=44100,
        channels=1,
        bit_depth=None,
        duration=2.0,
        bitrate=32000,
    ),
    AudioFixture(
        name="frames.m4a",
        inputs=_tone(rate=48000, seconds=2),
        outputs=("-c:a", "aac", "-b:a", "32k", *_FASTSTART),
        format_name=MP4_FORMAT,
        codec="aac",
        sample_rate=48000,
        channels=1,
        bit_depth=None,
        duration=2.0,
        bitrate=32000,
    ),
)
"""Sources for :data:`FRAME_CUTS`: a CBR MP3 with an Info header and a
faststart AAC M4A, both ending in audio data. They are ordinary files, not
sized against the 2 % cut, so they are kept out of :data:`FIXTURES`.
"""


@dataclass(frozen=True, slots=True)
class FrameCut:
    """A fixture with its last audio frames removed.

    The header still declares the full length. The cut is at the byte offset
    ffprobe reports for the first removed packet.

    Args:
        name: File name inside the fixture directory.
        source: Name of the healthy fixture it is cut from.
        frames: Number of audio frames removed from the end.
        detected: Whether the reader reports ``truncated=true`` on FFmpeg
            9.0.2. Losses it misses are a known limit: encoder padding at the
            end of MP3 and AAC hides them.
    """

    name: str
    source: str
    frames: int
    detected: bool


FRAME_CUTS: Final[tuple[FrameCut, ...]] = (
    FrameCut(name="frames_minus1.mp3", source="frames.mp3", frames=1, detected=False),
    FrameCut(name="frames_minus2.mp3", source="frames.mp3", frames=2, detected=True),
    FrameCut(name="frames_minus3.mp3", source="frames.mp3", frames=3, detected=True),
    FrameCut(name="frames_minus1.m4a", source="frames.m4a", frames=1, detected=False),
    FrameCut(name="frames_minus2.m4a", source="frames.m4a", frames=2, detected=True),
    FrameCut(name="frames_minus3.m4a", source="frames.m4a", frames=3, detected=True),
)
"""Fixtures with their last one, two, and three frames removed."""

CHAINED_OGG: Final[str] = "chained.ogg"
"""Two one-second Ogg Opus links with different serial numbers, back to back."""

_CHAIN_SERIALS: Final[tuple[str, ...]] = ("100", "200")


_TOOL_TIMEOUT_SECONDS: Final[float] = 120.0


def _tool(name: str) -> str:
    """Resolve an FFmpeg tool or stop.

    Args:
        name: ``ffmpeg`` or ``ffprobe``.

    Returns:
        The absolute binary path.

    Raises:
        SystemExit: When the tool is not installed.
    """
    binary = find_binary(name)
    if binary is None:
        raise SystemExit(f"{name} is required to generate the audio fixtures")
    return binary


def _run(argv: list[str], dest: Path) -> str:
    """Run an FFmpeg tool through winnow's runner and return its stdout.

    Args:
        argv: Fixed argument vector with an absolute binary path.
        dest: File the command writes or reads, for error messages.

    Returns:
        Captured stdout.

    Raises:
        SystemExit: When the tool fails.
    """
    completed = run_command(
        argv=argv,
        timeout=_TOOL_TIMEOUT_SECONDS,
        operation="generate_audio_fixtures",
        path=dest,
    )
    if completed.returncode != 0:
        raise SystemExit(f"{argv[0]} failed for {dest}: {completed.stderr}")
    return completed.stdout


def _ffmpeg(*, inputs: tuple[str, ...], outputs: tuple[str, ...], dest: Path) -> None:
    """Run ffmpeg with a fixed argument vector.

    Args:
        inputs: Input arguments.
        outputs: Output arguments.
        dest: Output file.
    """
    argv = [
        _tool(FFMPEG),
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
        str(dest.absolute()),
    ]
    _run(argv, dest)


def _packet_offsets(source: Path) -> list[int]:
    """Return the byte offset of each audio packet, as ffprobe reports it.

    Args:
        source: A generated fixture.

    Returns:
        Offsets in packet order.
    """
    stdout = _run(
        [
            _tool(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=pos",
            "-of",
            "json",
            f"file:{source.absolute()}",
        ],
        source,
    )
    packets = json.loads(stdout)["packets"]
    return [int(packet["pos"]) for packet in packets]


def _cut_frames(cut: FrameCut, directory: Path) -> Path:
    """Write ``cut``: its source without the last ``cut.frames`` packets.

    Args:
        cut: The frame cut.
        directory: Directory holding the source and receiving the cut.

    Returns:
        The written path.
    """
    source = directory / cut.source
    offset = _packet_offsets(source)[-cut.frames]
    dest = directory / cut.name
    dest.write_bytes(source.read_bytes()[:offset])
    return dest


def generate(directory: Path) -> list[Path]:
    """Write every audio fixture into ``directory``.

    Args:
        directory: Existing output directory.

    Returns:
        Paths written.
    """
    written: list[Path] = []
    for fixture in (*FIXTURES, *INCONSISTENT_FIXTURES, *FRAME_SOURCES):
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
    written.extend(_cut_frames(cut, directory) for cut in FRAME_CUTS)
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
