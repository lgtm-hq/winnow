"""Record real ffprobe output for the audio tests that run without ffprobe.

Run with
``uv run python tests/media/fixtures/audio/record_ffprobe_outputs.py`` after
``generate_audio_fixtures.py``. Each recording runs the real reader, captures
exactly what its two ffprobe calls returned (exit status, stdout, stderr),
and writes it to ``recorded/<case>.json``. The tests replay these through a
fake runner, so CI exercises every rule on genuine ffprobe output without an
ffprobe binary.

Truncated cases are cut from the generated fixtures in a temporary directory;
the cut copies are never committed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final
from unittest import mock

from winnow.media import _ffprobe
from winnow.media import audio as audio_module
from winnow.media._ffprobe import BoundedRun

FIXTURE_DIR: Final[Path] = Path(__file__).resolve().parent
RECORDED_DIR: Final[Path] = FIXTURE_DIR / "recorded"

CASES: Final[dict[str, tuple[str, float]]] = {
    "mp3": ("mp3.mp3", 1.0),
    "adts": ("adts.aac", 1.0),
    "alac": ("alac.m4a", 1.0),
    "chained_ogg": ("chained.ogg", 1.0),
    "cover_mp3": ("cover.mp3", 1.0),
    "two_tracks": ("two_tracks.mka", 1.0),
    "delayed_mkv": ("delayed.mkv", 1.0),
    "delayed_mp4": ("delayed.mp4", 1.0),
    "delayed_avi": ("delayed.avi", 1.0),
    "video_wmv": ("video.wmv", 1.0),
    "flac_cut_98": ("flac.flac", 0.98),
    "wav_cut_90": ("wav.wav", 0.9),
    "mp3_cut_50": ("mp3.mp3", 0.5),
    "cover_m4a": ("cover.m4a", 1.0),
    "timecode_mov": ("timecode.mov", 1.0),
    "timecode_mov_cut_90": ("timecode.mov", 0.9),
    "offset_mka": ("offset.mka", 1.0),
    "delay_moov": ("delay_moov.mp4", 1.0),
    "wma_mka": ("wma.mka", 1.0),
    "mp3_frag": ("mp3_frag.mp4", 1.0),
    "empty_m4a": ("empty.m4a", 1.0),
    "aac_frag": ("aac_frag.mp4", 1.0),
    "vorbis_frag": ("vorbis_frag.mp4", 1.0),
    "opus_frag": ("opus_frag.mp4", 1.0),
    "aac_frag_minus1": ("aac_frag_minus1.mp4", 1.0),
    "aac_frag_minus3": ("aac_frag_minus3.mp4", 1.0),
    "aac_frag_minus10": ("aac_frag_minus10.mp4", 1.0),
    "mp3_frag_minus1": ("mp3_frag_minus1.mp4", 1.0),
    "mp3_frag_minus3": ("mp3_frag_minus3.mp4", 1.0),
    "mp3_frag_minus10": ("mp3_frag_minus10.mp4", 1.0),
    "aac_fragments": ("aac_fragments.mp4", 1.0),
    "aac_fragments_cut": ("aac_fragments_cut.mp4", 1.0),
    "alac_fragments": ("alac_fragments.mp4", 1.0),
    "alac_fragments_cut": ("alac_fragments_cut.mp4", 1.0),
    "pcm_moov_fragments": ("pcm_moov_fragments.mp4", 1.0),
    "pcm_moov_fragments_cut": ("pcm_moov_fragments_cut.mp4", 1.0),
    "alac_moov_fragments": ("alac_moov_fragments.mp4", 1.0),
    "alac_moov_fragments_cut": ("alac_moov_fragments_cut.mp4", 1.0),
    "alac_indexed": ("alac_indexed.mp4", 1.0),
    "alac_indexed_cut": ("alac_indexed_cut.mp4", 1.0),
    "alac_bframes": ("alac_bframes.mp4", 1.0),
    "heaac": ("heaac.m4a", 1.0),
    "heaac_mono": ("heaac_mono.m4a", 1.0),
}
"""Recording name to (fixture, fraction of the file kept)."""

GARBAGE_CASE: Final[str] = "garbage"
GARBAGE_BYTES: Final[bytes] = b"\x00" * 4096


def _recording_runner(
    calls: list[dict[str, object]],
) -> Callable[..., BoundedRun]:
    """Wrap the real runner so every call's output is kept.

    Args:
        calls: Receives one entry per ffprobe call.

    Returns:
        A drop-in replacement for :func:`run_bounded`.
    """
    real = _ffprobe.run_bounded

    def record(
        argv: list[str],
        *,
        stdin: int,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        on_stdout_line: Callable[[bytes], None] | None = None,
    ) -> BoundedRun:
        lines: list[bytes] = []
        tee: Callable[[bytes], None] | None = None
        if on_stdout_line is not None:
            forward = on_stdout_line

            def tee(line: bytes) -> None:
                lines.append(line)
                forward(line)

        run = real(
            argv,
            stdin=stdin,
            timeout=timeout,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            on_stdout_line=tee,
        )
        stdout = run.stdout if tee is None else b"".join(line + b"\n" for line in lines)
        kind = "walk" if "-select_streams" in argv else "header"
        calls.append(
            {
                "kind": kind,
                "returncode": run.returncode,
                "stdout": stdout.decode("utf-8"),
                "stderr": run.stderr.decode("utf-8"),
            },
        )
        return run

    return record


def record_case(name: str, source: Path, directory: Path) -> Path:
    """Run the reader on ``source`` and save both ffprobe calls.

    Args:
        name: Recording name.
        source: File to probe.
        directory: Output directory.

    Returns:
        Path of the recording.
    """
    calls: list[dict[str, object]] = []
    with mock.patch.object(audio_module, "run_bounded", _recording_runner(calls)):
        audio_module.extract_audio_metadata(source)
    binary = _ffprobe.find_binary(_ffprobe.FFPROBE) or ""
    payload = {
        "ffprobe_version": _ffprobe.ffprobe_version(binary),
        "calls": calls,
    }
    out = directory / f"{name}.json"
    out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return out


def main() -> None:
    """Record every case into ``recorded/`` or the given directory."""
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else RECORDED_DIR
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for name, (fixture, fraction) in CASES.items():
            data = (FIXTURE_DIR / fixture).read_bytes()
            cut = Path(scratch) / f"{name}.bin"
            cut.write_bytes(data[: int(len(data) * fraction)])
            print(record_case(name, cut, directory))
        garbage = Path(scratch) / "garbage.bin"
        garbage.write_bytes(GARBAGE_BYTES)
        print(record_case(GARBAGE_CASE, garbage, directory))


if __name__ == "__main__":
    main()
