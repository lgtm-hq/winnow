"""Small audio samples for the audio backend tests, written at test time.

``write_ffmpeg_sample`` asks the system ``ffmpeg`` for a 0.3 s sine tone in one
container and codec. ``write_id3_sample`` needs no external program: it puts a
hand-built ID3v2 tag in front of the MPEG frames of the committed
``sample.mp3`` fixture, so a test knows exactly which frames are present.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from winnow.media.video import _run

_STEREO_44K = "sine=frequency=440:sample_rate=44100,aformat=channel_layouts=stereo"
_STEREO_48K = "sine=frequency=440:sample_rate=48000,aformat=channel_layouts=stereo"
_MONO_8K = "sine=frequency=440:sample_rate=8000"


@dataclass(frozen=True, slots=True)
class FfmpegSample:
    """How to write one sample with ffmpeg."""

    filename: str
    source: str
    codec_args: tuple[str, ...]


FFMPEG_SAMPLES: dict[str, FfmpegSample] = {
    "mp3": FfmpegSample("tone.mp3", _STEREO_44K, ("-c:a", "libmp3lame", "-b:a", "64k")),
    "flac": FfmpegSample("tone.flac", _STEREO_44K, ("-c:a", "flac")),
    "wav": FfmpegSample("tone.wav", _MONO_8K, ("-c:a", "pcm_s16le")),
    "m4a-aac": FfmpegSample("aac.m4a", _STEREO_44K, ("-c:a", "aac", "-b:a", "64k")),
    "m4a-alac": FfmpegSample("alac.m4a", _STEREO_44K, ("-c:a", "alac")),
    "opus": FfmpegSample("tone.opus", _STEREO_48K, ("-c:a", "libopus", "-b:a", "64k")),
    "aiff": FfmpegSample("tone.aiff", _MONO_8K, ("-c:a", "pcm_s16be")),
    "adts-aac": FfmpegSample(
        "tone.aac",
        _STEREO_44K,
        ("-c:a", "aac", "-b:a", "64k", "-f", "adts"),
    ),
    "wav-adpcm": FfmpegSample("adpcm.wav", _MONO_8K, ("-c:a", "adpcm_ms")),
}


def write_ffmpeg_sample(directory: Path, key: str) -> Path:
    """Write one ffmpeg sample into ``directory``, skipping without ffmpeg.

    Args:
        directory: Directory to write into.
        key: Key into ``FFMPEG_SAMPLES``.

    Returns:
        Path to the written sample.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")
    sample = FFMPEG_SAMPLES[key]
    output = directory / sample.filename
    if output.exists():
        return output
    argv = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        sample.source,
        "-t",
        "0.3",
        "-metadata",
        "title=Tone",
        "-metadata",
        "artist=Winnow",
        "-metadata",
        "album=Samples",
        "-metadata",
        "date=2019-07-04",
        *sample.codec_args,
        str(output),
    ]
    result = _run(argv=argv, timeout=60, operation="write_sample", path=output)
    if result.returncode != 0:
        pytest.skip(f"ffmpeg cannot write {key}: {result.stderr.strip()[-200:]}")
    return output


def _syncsafe(value: int) -> bytes:
    """Encode an ID3v2 syncsafe integer (7 bits per byte)."""
    return bytes(
        [(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F],
    )


def _id3_frame(frame_id: str, text: str, *, version: int) -> bytes:
    """Encode one Latin-1 text frame for ID3v2.3 or ID3v2.4."""
    payload = b"\x00" + text.encode("latin-1")
    size = _syncsafe(len(payload)) if version == 4 else len(payload).to_bytes(4, "big")
    return frame_id.encode("ascii") + size + b"\x00\x00" + payload


def _mpeg_frames(mp3_path: Path) -> bytes:
    """Return the bytes of ``mp3_path`` after any leading ID3v2 tag."""
    data = mp3_path.read_bytes()
    if data[:3] != b"ID3":
        return data
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    return data[10 + size :]


def write_id3_sample(
    output: Path,
    *,
    mp3_path: Path,
    frames: list[tuple[str, str]],
    version: int,
) -> Path:
    """Write an MP3 whose only tag is an ID3v2 tag with exactly ``frames``.

    Args:
        output: File to write.
        mp3_path: MP3 whose MPEG frames are reused.
        frames: ``(frame id, text)`` pairs, in order.
        version: ID3v2 major version, 3 or 4.

    Returns:
        ``output``.
    """
    body = b"".join(
        _id3_frame(frame_id, text, version=version) for frame_id, text in frames
    )
    header = b"ID3" + bytes([version, 0, 0]) + _syncsafe(len(body))
    output.write_bytes(header + body + _mpeg_frames(mp3_path))
    return output
