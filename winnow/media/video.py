"""Video metadata extraction and frame capture via FFmpeg.

FFmpeg and ffprobe are external binaries invoked through ``subprocess`` with a
fixed argument vector (never ``shell=True``). When the binaries are absent,
metadata extraction degrades gracefully to empty metadata and callers can probe
availability with :func:`ffmpeg_available` / :func:`ffprobe_available`.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed argv lists only; never shell=True
from fractions import Fraction
from pathlib import Path
from typing import Final

from loguru import logger

from winnow.exceptions import MediaError
from winnow.media._coerce import (
    coerce_non_negative_float,
    coerce_non_negative_int,
)
from winnow.models.media import MediaMetadata

_FFPROBE: Final[str] = "ffprobe"
_FFMPEG: Final[str] = "ffmpeg"
_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0
_FRAME_TIMEOUT_SECONDS: Final[float] = 60.0


def ffprobe_available() -> bool:
    """Report whether the ``ffprobe`` binary is on ``PATH``.

    Returns:
        ``True`` when ffprobe can be resolved.
    """
    return shutil.which(_FFPROBE) is not None


def ffmpeg_available() -> bool:
    """Report whether the ``ffmpeg`` binary is on ``PATH``.

    Returns:
        ``True`` when ffmpeg can be resolved.
    """
    return shutil.which(_FFMPEG) is not None


def extract_video_metadata(path: Path) -> MediaMetadata:
    """Extract metadata from a video file using ffprobe.

    When ffprobe is unavailable the call degrades gracefully: an empty
    :class:`MediaMetadata` is returned and a warning is logged. Callers that need
    to distinguish "no FFmpeg" from "no metadata" can check
    :func:`ffprobe_available`.

    Args:
        path: Filesystem path to the video.

    Returns:
        Metadata populated with duration, resolution, codec, bitrate, and frame
        rate where ffprobe can determine them.

    Raises:
        MediaError: If the file is missing, or ffprobe is present but fails to
            probe the file.
    """
    if not path.is_file():
        raise MediaError(
            "video file does not exist",
            operation="extract_video_metadata",
            file_path=path,
        )

    binary = shutil.which(_FFPROBE)
    if binary is None:
        logger.warning(
            "ffprobe not found; returning empty metadata for {}",
            path,
        )
        return MediaMetadata()

    argv = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = _run(
        argv=argv,
        timeout=_PROBE_TIMEOUT_SECONDS,
        operation="extract_video_metadata",
        path=path,
    )
    if completed.returncode != 0:
        raise MediaError(
            "ffprobe failed to read video",
            operation="extract_video_metadata",
            file_path=path,
            details={"stderr": completed.stderr.strip()},
        )

    return _parse_ffprobe_output(payload=completed.stdout, path=path)


def read_video_tags(path: Path) -> dict[str, str]:
    """Read container-level tags from a video file using ffprobe.

    Returns the ``format.tags`` mapping reported by ``ffprobe -show_format``
    with lower-cased keys. The call never raises for a readable file: a missing
    ffprobe, a non-zero exit, unparseable output, or an absent ``tags`` block
    all degrade to an empty mapping.

    Args:
        path: Filesystem path to the video.

    Returns:
        Mapping of lower-cased tag key to its stringified value. Empty when
        ffprobe is missing, cannot be launched, times out, exits non-zero, or
        reports no tags.

    Raises:
        MediaError: If the path is not a file.
    """
    if not path.is_file():
        raise MediaError(
            "video file does not exist",
            operation="read_video_tags",
            file_path=path,
        )

    binary = shutil.which(_FFPROBE)
    if binary is None:
        logger.debug("ffprobe not found; returning no tags for {}", path)
        return {}

    argv = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        str(path),
    ]
    try:
        completed = _run(
            argv=argv,
            timeout=_PROBE_TIMEOUT_SECONDS,
            operation="read_video_tags",
            path=path,
        )
    except MediaError as exc:
        logger.debug("ffprobe could not run for {}: {}", path, exc)
        return {}
    if completed.returncode != 0:
        logger.debug(
            "ffprobe failed to read tags for {}: {}",
            path,
            completed.stderr.strip(),
        )
        return {}

    return _parse_format_tags(payload=completed.stdout, path=path)


def extract_frame(
    path: Path,
    destination: Path,
    *,
    timestamp_seconds: float = 0.0,
) -> Path:
    """Extract a single frame from a video to an image file.

    Args:
        path: Source video path.
        destination: Path where the extracted frame image is written. The image
            format is inferred from its suffix.
        timestamp_seconds: Seek position in seconds for the captured frame.

    Returns:
        The destination path that was written.

    Raises:
        MediaError: If ffmpeg is unavailable, the source is missing, the
            timestamp is negative, or the extraction fails.
    """
    if not path.is_file():
        raise MediaError(
            "video file does not exist",
            operation="extract_frame",
            file_path=path,
        )

    if timestamp_seconds < 0:
        raise MediaError(
            "timestamp_seconds must be non-negative",
            operation="extract_frame",
            file_path=path,
            details={"timestamp_seconds": timestamp_seconds},
        )

    binary = shutil.which(_FFMPEG)
    if binary is None:
        raise MediaError(
            "ffmpeg not found; cannot extract frame",
            operation="extract_frame",
            file_path=path,
        )

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaError(
            "failed to create destination directory for frame extraction",
            operation="extract_frame",
            file_path=destination,
            details={"error": str(exc)},
        ) from exc
    argv = [
        binary,
        "-y",
        "-ss",
        str(timestamp_seconds),
        "-i",
        str(path),
        "-frames:v",
        "1",
        str(destination),
    ]
    completed = _run(
        argv=argv,
        timeout=_FRAME_TIMEOUT_SECONDS,
        operation="extract_frame",
        path=path,
    )
    if completed.returncode != 0 or not destination.is_file():
        raise MediaError(
            "ffmpeg failed to extract frame",
            operation="extract_frame",
            file_path=path,
            details={"stderr": completed.stderr.strip()},
        )
    return destination


def _run(
    *,
    argv: list[str],
    timeout: float,
    operation: str,
    path: Path,
) -> subprocess.CompletedProcess[str]:
    """Run an FFmpeg-family command with a fixed argument vector.

    Args:
        argv: Fully-resolved command and arguments; ``argv[0]`` is an absolute
            binary path from ``shutil.which``.
        timeout: Maximum seconds to wait for completion.
        operation: Operation name used in raised errors.
        path: Media path used in raised errors.

    Returns:
        The completed process, including captured stdout and stderr.

    Raises:
        MediaError: If the process cannot be launched or times out.
    """
    try:
        return subprocess.run(  # nosec B603 - fixed argv list from which(); no shell
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            "FFmpeg command timed out",
            operation=operation,
            file_path=path,
        ) from exc
    except OSError as exc:
        raise MediaError(
            "failed to launch FFmpeg command",
            operation=operation,
            file_path=path,
        ) from exc


def _parse_ffprobe_output(*, payload: str, path: Path) -> MediaMetadata:
    """Parse ffprobe JSON output into :class:`MediaMetadata`.

    Args:
        payload: Raw JSON string emitted by ffprobe.
        path: Media path used in raised errors.

    Returns:
        Metadata derived from the first video stream and container format.

    Raises:
        MediaError: If the JSON payload cannot be decoded.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MediaError(
            "could not parse ffprobe output",
            operation="extract_video_metadata",
            file_path=path,
        ) from exc

    streams = data.get("streams", [])
    video_stream: dict[str, object] = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        {},
    )
    container: dict[str, object] = data.get("format", {})

    duration = coerce_non_negative_float(
        _first_available(video_stream.get("duration"), container.get("duration")),
    )
    bitrate = coerce_non_negative_int(
        _first_available(video_stream.get("bit_rate"), container.get("bit_rate")),
        via_float=True,
    )
    codec = video_stream.get("codec_name")

    return MediaMetadata(
        width=coerce_non_negative_int(video_stream.get("width"), via_float=True),
        height=coerce_non_negative_int(video_stream.get("height"), via_float=True),
        duration_seconds=duration,
        codec=codec if isinstance(codec, str) else None,
        bitrate=bitrate,
        frame_rate=_parse_frame_rate(video_stream.get("avg_frame_rate")),
    )


def _parse_format_tags(*, payload: str, path: Path) -> dict[str, str]:
    """Extract ``format.tags`` from ffprobe JSON output.

    Args:
        payload: Raw JSON string emitted by ffprobe.
        path: Media path used in debug logging.

    Returns:
        Lower-cased tag mapping, or an empty mapping when the payload cannot be
        decoded or carries no tags.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.debug("could not parse ffprobe tag output for {}: {}", path, exc)
        return {}

    container = data.get("format", {}) if isinstance(data, dict) else {}
    tags = container.get("tags", {}) if isinstance(container, dict) else {}
    if not isinstance(tags, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in tags.items()}


def _first_available(*values: object) -> object:
    """Return the first ffprobe value that is present and usable.

    ffprobe reports missing stream-level fields as the literal string
    ``"N/A"``, which is truthy and would otherwise mask a real value in the
    container-level fallback.

    Args:
        values: Candidate values in priority order.

    Returns:
        The first value that is neither None nor ``"N/A"``, or None.
    """
    for value in values:
        if value is None or value == "N/A":
            continue
        return value
    return None


def _parse_frame_rate(value: object) -> float | None:
    """Convert an ffprobe frame-rate fraction to frames per second.

    Args:
        value: Frame-rate expression such as ``"30000/1001"`` or ``"0/0"``.

    Returns:
        Frames per second, or ``None`` when the value is missing or degenerate.
    """
    if not isinstance(value, str) or "/" not in value:
        return None
    try:
        fraction = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    if fraction <= 0:
        return None
    return float(fraction)
