"""Stream types from guarded ffprobe, for detection rules 3 and 4 (#393).

Matroska and WebM carry no media type in their header, and an MPEG
transport stream's program tables may be missing from its first bytes. For
those, detection asks ffprobe which streams the file holds. The call goes
through the shared runner in :mod:`winnow.media._ffprobe`
(:func:`~winnow.media._ffprobe.run_bounded`): a fixed argument vector, one
demuxer on the allow-list, only the ``fd`` protocol, bounded time and
output, never retried. ffprobe reads the descriptor detection already holds
open (``-fd 0 fd:``), never the file's name.

Without ffprobe, or with an ffprobe that lacks the ``fd`` protocol, the
check is unavailable and the caller reports unknown with a signal saying
so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Final

from winnow.media._ffprobe import (
    FFPROBE,
    BoundedRunError,
    fd_protocol_supported,
    find_binary,
    run_bounded,
)
from winnow.models.media import MediaType

PROBE_TIMEOUT_SECONDS: Final[float] = 2.0
"""Longest a stream-type probe may run."""

PROBE_STDOUT_LIMIT: Final[int] = 1024 * 1024
"""Most stdout bytes a stream-type probe may write."""

PROBE_STDERR_LIMIT: Final[int] = 64 * 1024
"""Most stderr bytes a stream-type probe may write."""

_SHOW_ENTRIES: Final[str] = "stream=index,codec_type:stream_disposition=attached_pic"

_CODEC_TYPES: Final[dict[str, MediaType]] = {
    "video": MediaType.VIDEO,
    "audio": MediaType.AUDIO,
}


@dataclass(frozen=True, slots=True)
class StreamProbe:
    """What ffprobe established about a file's streams.

    Args:
        media_type: Video when a video stream that is not cover art exists,
            audio when no such stream exists and an audio stream does,
            otherwise ``None``.
        detail: The evidence, or why the check was unavailable or failed.
        available: Whether ffprobe could inspect the file at all.
    """

    media_type: MediaType | None
    detail: str
    available: bool


def probe_stream_types(descriptor: int, *, demuxer: str) -> StreamProbe:
    """Ask ffprobe for the stream types of an open file.

    Args:
        descriptor: Open descriptor of the regular file; it is rewound
            first and its offset is left wherever ffprobe stopped.
        demuxer: The only demuxer ffprobe may use, such as ``"matroska"``.

    Returns:
        The stream evidence, or an unavailable or failed check.
    """
    binary = find_binary(FFPROBE)
    if binary is None:
        return StreamProbe(None, "unavailable=ffprobe not found on PATH", False)
    if not fd_protocol_supported(binary):
        return StreamProbe(None, "unavailable=ffprobe lacks the fd protocol", False)
    argv = [
        binary,
        "-hide_banner",
        "-v",
        "error",
        "-protocol_whitelist",
        "fd",
        "-format_whitelist",
        demuxer,
        "-show_entries",
        _SHOW_ENTRIES,
        "-of",
        "json",
        "-fd",
        "0",
        "fd:",
    ]
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        run = run_bounded(
            argv,
            stdin=descriptor,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdout_limit=PROBE_STDOUT_LIMIT,
            stderr_limit=PROBE_STDERR_LIMIT,
        )
    except (OSError, BoundedRunError) as exc:
        return StreamProbe(None, f"unavailable=ffprobe could not run: {exc}", False)
    if run.breached:
        return StreamProbe(None, "ffprobe=exceeded its time or output bound", True)
    if run.returncode != 0:
        return StreamProbe(None, f"ffprobe=exit {run.returncode}", True)
    return _interpret(run.stdout)


def _interpret(stdout: bytes) -> StreamProbe:
    """Classify ffprobe's JSON stream list.

    Args:
        stdout: ffprobe's JSON output.

    Returns:
        The stream evidence.
    """
    try:
        data = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return StreamProbe(None, "ffprobe=unreadable output", True)
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        return StreamProbe(None, "ffprobe=no stream list", True)
    kinds: list[str] = []
    for stream in streams:
        if not isinstance(stream, dict):
            return StreamProbe(None, "ffprobe=malformed stream entry", True)
        kind = stream.get("codec_type")
        disposition = stream.get("disposition")
        cover = isinstance(disposition, dict) and disposition.get("attached_pic") == 1
        kinds.append("cover" if cover and kind == "video" else str(kind))
    listed = ",".join(kinds) or "(none)"
    for kind, media_type in _CODEC_TYPES.items():
        if kind in kinds:
            return StreamProbe(media_type, f"ffprobe streams={listed}", True)
    return StreamProbe(None, f"ffprobe streams={listed}", True)


__all__ = [
    "PROBE_STDERR_LIMIT",
    "PROBE_STDOUT_LIMIT",
    "PROBE_TIMEOUT_SECONDS",
    "StreamProbe",
    "probe_stream_types",
]
