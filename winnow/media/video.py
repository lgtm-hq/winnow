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
        MediaError: If ffmpeg is unavailable, the source is missing, or the
            extraction fails.
    """
    if not path.is_file():
        raise MediaError(
            "video file does not exist",
            operation="extract_frame",
            file_path=path,
        )

    binary = shutil.which(_FFMPEG)
    if binary is None:
        raise MediaError(
            "ffmpeg not found; cannot extract frame",
            operation="extract_frame",
            file_path=path,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
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

    duration = _parse_float(video_stream.get("duration") or container.get("duration"))
    bitrate = _parse_int(video_stream.get("bit_rate") or container.get("bit_rate"))
    codec = video_stream.get("codec_name")

    return MediaMetadata(
        width=_parse_int(video_stream.get("width")),
        height=_parse_int(video_stream.get("height")),
        duration_seconds=duration,
        codec=codec if isinstance(codec, str) else None,
        bitrate=bitrate,
        frame_rate=_parse_frame_rate(video_stream.get("avg_frame_rate")),
    )


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


def _parse_int(value: object) -> int | None:
    """Parse a value into a non-negative integer.

    Args:
        value: Raw ffprobe field value.

    Returns:
        Parsed integer, or ``None`` when it is missing or invalid.
    """
    if value is None:
        return None
    try:
        parsed = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_float(value: object) -> float | None:
    """Parse a value into a non-negative float.

    Args:
        value: Raw ffprobe field value.

    Returns:
        Parsed float, or ``None`` when it is missing or invalid.
    """
    if value is None:
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
