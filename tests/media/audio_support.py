"""Shared builders for the audio reader tests."""

from __future__ import annotations

import pytest

from winnow.media._ffprobe import BoundedRun
from winnow.models.media import AudioFlag, MediaMetadata


def run_result(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
    overflowed: bool = False,
) -> BoundedRun:
    """Build a finished run.

    Args:
        returncode: Exit status.
        stdout: Captured stdout.
        stderr: Captured stderr.
        timed_out: Whether the time-out expired.
        overflowed: Whether an output bound was exceeded.

    Returns:
        The run.
    """
    return BoundedRun(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        overflowed=overflowed,
    )


def audio_stream(**overrides: object) -> dict[str, object]:
    """Build an ffprobe audio stream object.

    Args:
        overrides: Keys to replace or add; a value of ``None`` removes a key.

    Returns:
        The stream object.
    """
    stream: dict[str, object] = {
        "index": 0,
        "codec_name": "flac",
        "codec_type": "audio",
        "sample_rate": "44100",
        "channels": 2,
        "time_base": "1/44100",
        "bits_per_raw_sample": "16",
        "bits_per_sample": 0,
        "duration": "10.000000",
        "bit_rate": "900000",
        "disposition": {"default": 0, "attached_pic": 0},
    }
    for key, value in overrides.items():
        if value is None:
            stream.pop(key, None)
        else:
            stream[key] = value
    return stream


def probe_json(
    *streams: dict[str, object],
    **format_overrides: object,
) -> dict[str, object]:
    """Build an ffprobe header object.

    Args:
        streams: Stream objects; one default FLAC stream when empty.
        format_overrides: Keys to replace in ``format``.

    Returns:
        The header object.
    """
    container: dict[str, object] = {
        "format_name": "flac",
        "duration": "10.000000",
        "start_time": "0.000000",
        "probe_score": 100,
    }
    container.update(format_overrides)
    return {"streams": list(streams) or [audio_stream()], "format": container}


def known_duration(metadata: MediaMetadata) -> float:
    """Return the reported duration, failing the test when it is unknown.

    Args:
        metadata: Reader output.

    Returns:
        The duration in seconds.
    """
    if metadata.duration_seconds is None:
        pytest.fail("duration is unknown")
    return metadata.duration_seconds


def known_flags(metadata: MediaMetadata) -> tuple[AudioFlag, ...]:
    """Return the audio flags, failing the test when the reader set none.

    Args:
        metadata: Reader output.

    Returns:
        The flags.
    """
    if metadata.audio_flags is None:
        pytest.fail("audio flags were not set")
    return metadata.audio_flags


def known_reason(metadata: MediaMetadata) -> str:
    """Return the backend reason, failing the test when there is none.

    Args:
        metadata: Reader output.

    Returns:
        The reason.
    """
    if metadata.audio_backend_reason is None:
        pytest.fail("no backend reason")
    return metadata.audio_backend_reason
