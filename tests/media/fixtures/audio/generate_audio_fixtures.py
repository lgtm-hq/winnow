"""Regenerate the committed audio fixtures in this directory.

Run by hand, never at test time::

    uv run python tests/media/fixtures/audio/generate_audio_fixtures.py

Each fixture is a 0.3 s synthetic 440 Hz sine tone written by the system
``ffmpeg`` from its ``lavfi`` ``sine`` source, with a fixed set of tags. The
tests read the committed files, so CI needs no ffmpeg; regenerating with a
different ffmpeg build changes encoder strings and can shift recorded values,
so update ``tests/media/test_audio.py`` together with the files.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from winnow.media.video import _run

_DIRECTORY = Path(__file__).resolve().parent

_STEREO_44K = "sine=frequency=440:sample_rate=44100,aformat=channel_layouts=stereo"
_STEREO_48K = "sine=frequency=440:sample_rate=48000,aformat=channel_layouts=stereo"
_MONO_8K = "sine=frequency=440:sample_rate=8000"

_TAGS: tuple[str, ...] = (
    "-metadata",
    "title=Tone",
    "-metadata",
    "artist=Winnow",
    "-metadata",
    "album=Samples",
    "-metadata",
    "date=2019-07-04",
    "-metadata",
    "track=3/9",
)


@dataclass(frozen=True, slots=True)
class AudioFixture:
    """How one committed fixture is written."""

    filename: str
    source: str
    codec_args: tuple[str, ...]


FIXTURES: tuple[AudioFixture, ...] = (
    AudioFixture("tone.mp3", _STEREO_44K, ("-c:a", "libmp3lame", "-b:a", "64k")),
    AudioFixture("tone.flac", _STEREO_44K, ("-c:a", "flac")),
    AudioFixture("tone.wav", _MONO_8K, ("-c:a", "pcm_s16le")),
    AudioFixture("aac.m4a", _STEREO_44K, ("-c:a", "aac", "-b:a", "64k")),
    AudioFixture("alac.m4a", _MONO_8K, ("-c:a", "alac")),
    AudioFixture("tone.opus", _STEREO_48K, ("-c:a", "libopus", "-b:a", "64k")),
    AudioFixture("tone.aiff", _MONO_8K, ("-c:a", "pcm_s16be")),
    AudioFixture("tone.aac", _STEREO_44K, ("-c:a", "aac", "-b:a", "64k", "-f", "adts")),
    AudioFixture("adpcm.wav", _MONO_8K, ("-c:a", "adpcm_ms")),
    AudioFixture("flac.oga", _MONO_8K, ("-c:a", "flac", "-f", "ogg")),
    AudioFixture("ima4.aifc", _MONO_8K, ("-c:a", "adpcm_ima_qt", "-f", "aiff")),
    AudioFixture("tone.wma", _MONO_8K, ("-c:a", "wmav2", "-b:a", "32k")),
    AudioFixture("tone.ac3", _MONO_8K, ("-c:a", "ac3", "-ar", "32000", "-b:a", "64k")),
    AudioFixture("tone.tta", _MONO_8K, ("-c:a", "tta")),
    AudioFixture("tone.wv", _MONO_8K, ("-c:a", "wavpack")),
)


def write_fixture(fixture: AudioFixture, directory: Path, ffmpeg: str) -> Path:
    """Write one fixture with ffmpeg.

    Args:
        fixture: The fixture to write.
        directory: Directory to write into.
        ffmpeg: Path to the ffmpeg binary.

    Returns:
        Path to the written file.

    Raises:
        RuntimeError: If ffmpeg fails.
    """
    output = directory / fixture.filename
    argv = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        fixture.source,
        "-t",
        "0.3",
        *_TAGS,
        *fixture.codec_args,
        str(output),
    ]
    completed = _run(argv=argv, timeout=60, operation="write_fixture", path=output)
    if completed.returncode != 0:
        message = f"ffmpeg cannot write {fixture.filename}: {completed.stderr}"
        raise RuntimeError(message)
    return output


def main() -> int:
    """Regenerate every fixture.

    Returns:
        Process exit status.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        sys.stderr.write("ffmpeg is not installed\n")
        return 1
    for fixture in FIXTURES:
        path = write_fixture(fixture, _DIRECTORY, ffmpeg)
        sys.stdout.write(f"{path.name}: {path.stat().st_size} bytes\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
