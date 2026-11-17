"""Audio facts from guarded ffprobe.

ffprobe is the only source of audio facts: container, codec, sample rate,
channels, bit depth, bitrate, and duration. No Python code here reads a byte
of the media file. The file is opened once and handed to ffprobe as its
stdin: through ``-fd 0 fd:``, or, on an FFmpeg without the ``fd`` protocol,
through an extension-less symlink in a private directory that points at
``/dev/fd/0`` (at the file's absolute path where ``/dev/fd`` does not exist),
so ffprobe never sees the file's name. Anything ffprobe does not establish is
reported as unknown.

The rules are those of issue #400 and its amendments A1 to A7. This module
runs ffprobe; the decisions on its output live in
:mod:`winnow.media._audio_rules`:

* R1/R2: one header call with a format allow-list; every fact is unknown
  unless the call exits 0, names an allowed container, has an audio stream
  with codec, sample rate, and channels, and writes only benign stderr lines
  (an unexpected line is ``unrecognized_output``, not ``corrupt``).
* R3: cover art is never a stream to read; the primary stream is the first
  default audio stream, else the first audio stream.
* R4/R5/R7: facts come from the primary stream; a declared duration is used
  only where the container really declares one and, for the end times
  (Matroska ``DURATION``, ``format.duration``), only when the file starts at
  zero; bitrate is the stream's own.
* R6: a packet walk of the primary stream checks the declared duration and
  measures it where none is declared.
* R8: both calls are bounded in time and output and are never retried.
* A6: the file's device, inode, size, and modification time are taken before
  the header call and after the walk; any change makes every fact unknown
  with ``inconsistent``.

Known limits (not worked around):

* Damage inside an intact packet is not detected; that needs a full decode.
* A damaged header byte can change a declared value (channels, sample rate,
  codec variant, duration) and leave no evidence; ``truncated=false`` means
  only that the audio present matches the declared length.
* A loss shorter than one packet leaves no evidence. Where a codec pads its
  last frames (MP3, AAC), the padding hides more: on FFmpeg 9.0.2, MP3 and
  faststart AAC M4A files with their last one or two frames removed still
  report ``truncated=false`` with the full declared duration; three frames
  are caught except at very low MP3 bitrates.
* MP3 is ``estimated`` only when ffprobe says it estimated the duration (no
  Xing/Info or VBRI header); with such a header the duration is declared and
  checked like any other.
* A cut exactly on an Ogg page, MPEG-TS packet, or raw ADTS/AC-3 frame
  boundary leaves no evidence; those formats are always ``estimated``, never
  ``truncated=false``.
* Chained Ogg is reported ``inconsistent``.
* A WAV written to a pipe is flagged; an MS ADPCM WAV whose ``fact`` chunk
  ffprobe ignores is ``estimated``.
* The file identity check (A6) cannot see a file that is changed and then
  restored with the same size and modification time.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loguru import logger

from winnow.exceptions import MediaError
from winnow.media._audio_rules import (
    ACCEPTED_FORMAT_NAMES,
    BENIGN_STDERR_BY_MAJOR,
    BIT_DEPTH_CODECS,
    ESTIMATE_STDERR_LINES,
    MATROSKA_FORMAT_NAME,
    NO_DECLARED_DURATION_FORMATS,
    DurationOutcome,
    PacketWalk,
    ProbeHeader,
    bit_depth_for,
    decide_duration,
    declared_duration,
    header_from_json,
    interpret_header,
    load_probe_json,
    parse_decimal,
    parse_positive_decimal,
    parse_positive_int,
    primary_audio_stream,
    stderr_lines,
)
from winnow.media._ffprobe import (
    FFPROBE,
    BoundedRunError,
    fd_protocol_supported,
    ffprobe_version,
    find_binary,
    parse_version_major,
    run_bounded,
)
from winnow.models.media import AudioFlag, MediaMetadata

FORMAT_WHITELIST: Final[str] = (
    "mp3,aac,mov,flac,ogg,wav,aiff,asf,wv,ac3,eac3,tta,matroska,avi,mpegts,mpeg,flv"
)
"""Demuxers ffprobe may use (``-format_whitelist``)."""

HEADER_TIMEOUT_SECONDS: Final[float] = 10.0
WALK_BASE_TIMEOUT_SECONDS: Final[float] = 10.0
WALK_BYTES_PER_EXTRA_SECOND: Final[int] = 20_000_000
WALK_MAX_TIMEOUT_SECONDS: Final[float] = 300.0
HEADER_STDOUT_LIMIT: Final[int] = 8 * 1024 * 1024
WALK_STDOUT_LIMIT: Final[int] = 256 * 1024 * 1024
STDERR_LIMIT: Final[int] = 64 * 1024

_DESCRIPTOR_DIR: Final[Path] = Path("/dev/fd")

FileIdentity = tuple[int, ...]
"""Device, inode, size, and modification time (ns) of the probed file."""


@dataclass(frozen=True, slots=True)
class _ProbeSource:
    """How ffprobe receives the file.

    Args:
        protocol: Value for ``-protocol_whitelist``.
        input_args: Trailing input arguments.
        stdin: Descriptor handed to ffprobe as stdin.
        size: File size in bytes.
        rewind: Resets the descriptor before each run.
        identity: Returns the file's current identity (A6).
    """

    protocol: str
    input_args: tuple[str, ...]
    stdin: int
    size: int
    rewind: Callable[[], None]
    identity: Callable[[], FileIdentity]


def extract_audio_metadata(path: Path) -> MediaMetadata:
    """Read audio facts from a file with guarded ffprobe.

    Facts ffprobe does not establish are ``None``; ``audio_flags`` says why
    and ``truncated`` says whether a packet walk checked the duration.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Metadata with the audio facts, flags, and truncation state.

    Raises:
        MediaError: If the path is not a regular file or cannot be opened,
            or if ffprobe's output could not be read.
    """
    if not path.is_file():
        raise MediaError(
            "audio file does not exist",
            operation="extract_audio_metadata",
            file_path=path,
        )
    binary = find_binary(FFPROBE)
    if binary is None:
        return _unavailable("ffprobe not found on PATH")
    benign = benign_stderr_lines(binary)
    with _probe_source(path=path, binary=binary) as source:
        try:
            return _checked_probe(binary=binary, source=source, benign=benign)
        except OSError as exc:
            logger.debug("ffprobe could not start for {}: {}", path, exc)
            return _unavailable(f"ffprobe could not start: {exc}")
        except BoundedRunError as exc:
            raise MediaError(
                "ffprobe output could not be read",
                operation="extract_audio_metadata",
                file_path=path,
                details={"error": str(exc.__cause__)},
            ) from exc


def benign_stderr_lines(binary: str) -> frozenset[str]:
    """Return the benign stderr lines pinned for the installed FFmpeg major.

    Args:
        binary: Absolute path of the ffprobe binary.

    Returns:
        The pinned lines, or an empty set for an unpinned or unknown major.
    """
    major = parse_version_major(ffprobe_version(binary) or "")
    if major is None:
        return frozenset()
    return BENIGN_STDERR_BY_MAJOR.get(major, frozenset())


def walk_timeout(size: int) -> float:
    """Return the packet-walk time-out for a file size (R8).

    Args:
        size: File size in bytes.

    Returns:
        10 s plus 1 s per 20 MB, capped at 300 s.
    """
    seconds = WALK_BASE_TIMEOUT_SECONDS + size / WALK_BYTES_PER_EXTRA_SECOND
    return min(seconds, WALK_MAX_TIMEOUT_SECONDS)


def _checked_probe(
    *,
    binary: str,
    source: _ProbeSource,
    benign: frozenset[str],
) -> MediaMetadata:
    """Run :func:`_probe` and discard its result if the file changed (A6).

    Args:
        binary: Absolute path of the ffprobe binary.
        source: How ffprobe receives the file.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The audio metadata, or all unknown with ``inconsistent`` when the
        file's identity differs after the last call from before the first.
    """
    before = source.identity()
    metadata = _probe(binary=binary, source=source, benign=benign)
    if source.identity() != before:
        return MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,))
    return metadata


def _probe(
    *,
    binary: str,
    source: _ProbeSource,
    benign: frozenset[str],
) -> MediaMetadata:
    """Run the header call and, when accepted, the packet walk.

    Args:
        binary: Absolute path of the ffprobe binary.
        source: How ffprobe receives the file.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The audio metadata.
    """
    source.rewind()
    header_run = run_bounded(
        [
            *_base_argv(binary, source),
            "-of",
            "json=sv=ignore",
            "-show_format",
            "-show_streams",
            *source.input_args,
        ],
        stdin=source.stdin,
        timeout=HEADER_TIMEOUT_SECONDS,
        stdout_limit=HEADER_STDOUT_LIMIT,
        stderr_limit=STDERR_LIMIT,
    )
    if header_run.breached:
        return MediaMetadata(audio_flags=(AudioFlag.TIMEOUT,))
    header, flags = interpret_header(header_run, benign=benign)
    if header is None:
        return MediaMetadata(audio_flags=flags)
    outcome, reason = _walk(binary=binary, source=source, header=header, benign=benign)
    estimated = AudioFlag.ESTIMATED in outcome.flags
    bitrate_known = outcome.duration is not None and not estimated
    return MediaMetadata(
        container=header.container,
        codec=header.codec,
        sample_rate=header.sample_rate,
        channels=header.channels,
        bit_depth=header.bit_depth,
        bitrate=header.bitrate if bitrate_known else None,
        duration_seconds=(
            float(outcome.duration) if outcome.duration is not None else None
        ),
        audio_stream_count=header.audio_stream_count,
        probe_score=header.probe_score,
        truncated=outcome.truncated,
        audio_flags=outcome.flags,
        audio_backend_reason=reason,
    )


def _walk(
    *,
    binary: str,
    source: _ProbeSource,
    header: ProbeHeader,
    benign: frozenset[str],
) -> tuple[DurationOutcome, str | None]:
    """Run the packet walk of the primary stream (R6, R8).

    Args:
        binary: Absolute path of the ffprobe binary.
        source: How ffprobe receives the file.
        header: Accepted header facts.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The duration outcome and, when ffprobe could not start, the reason.
    """
    walk = PacketWalk()
    source.rewind()
    try:
        run = run_bounded(
            [
                *_base_argv(binary, source),
                "-select_streams",
                str(header.stream_index),
                "-show_entries",
                "packet=pts_time,duration_time,flags",
                "-of",
                "csv=p=0",
                *source.input_args,
            ],
            stdin=source.stdin,
            timeout=walk_timeout(source.size),
            stdout_limit=WALK_STDOUT_LIMIT,
            stderr_limit=STDERR_LIMIT,
            on_stdout_line=walk.feed,
        )
    except OSError as exc:
        reason = f"ffprobe could not start: {exc}"
        return DurationOutcome(None, None, (AudioFlag.BACKEND_UNAVAILABLE,)), reason
    if run.breached:
        return DurationOutcome(None, None, (AudioFlag.TIMEOUT,)), None
    return decide_duration(header, walk, run, benign=benign), None


def _base_argv(binary: str, source: _ProbeSource) -> list[str]:
    """Return the guard arguments shared by both calls (R1).

    Args:
        binary: Absolute path of the ffprobe binary.
        source: How ffprobe receives the file.

    Returns:
        The leading arguments.
    """
    return [
        binary,
        "-hide_banner",
        "-v",
        "warning",
        "-protocol_whitelist",
        source.protocol,
        "-format_whitelist",
        FORMAT_WHITELIST,
    ]


@contextlib.contextmanager
def _probe_source(*, path: Path, binary: str) -> Iterator[_ProbeSource]:
    """Hand the file to ffprobe without its name (R1).

    The file is opened in both forms, so an unreadable file fails the same
    way, and the open descriptor is ffprobe's stdin. With the ``fd``
    protocol ffprobe reads it through ``-fd 0 fd:``. Otherwise ffprobe gets
    ``file:<absolute path>`` of a symlink named ``input`` in a private
    temporary directory; the link points at ``/dev/fd/0``, ffprobe's own
    stdin, so it reads the file that was opened even if the path is replaced
    later. Where ``/dev/fd`` does not exist the link points at the file's
    absolute path, and the path's identity is also compared with the
    descriptor's (A6).

    Args:
        path: The audio file.
        binary: Absolute path of the ffprobe binary.

    Yields:
        The probe source.

    Raises:
        MediaError: If the file cannot be opened or is not a regular file,
            or the link cannot be made.
    """
    with _open_descriptor(path) as (descriptor, size):
        if fd_protocol_supported(binary):
            yield _ProbeSource(
                protocol="fd",
                input_args=("-fd", "0", "fd:"),
                stdin=descriptor,
                size=size,
                rewind=lambda: _rewind(descriptor),
                identity=lambda: _identity(descriptor),
            )
            return
        with tempfile.TemporaryDirectory(prefix="winnow-ffprobe-") as private:
            link = Path(private) / "input"
            by_descriptor = _DESCRIPTOR_DIR.is_dir()
            try:
                link.symlink_to(
                    _DESCRIPTOR_DIR / "0" if by_descriptor else path.absolute(),
                )
            except OSError as exc:
                raise MediaError(
                    "cannot link audio file for ffprobe",
                    operation="extract_audio_metadata",
                    file_path=path,
                    details={"error": str(exc)},
                ) from exc
            yield _ProbeSource(
                protocol="file",
                input_args=(f"file:{link}",),
                stdin=descriptor,
                size=size,
                rewind=lambda: _rewind(descriptor),
                identity=(
                    (lambda: _identity(descriptor))
                    if by_descriptor
                    else (lambda: _identity(descriptor) + _path_identity(path))
                ),
            )


@contextlib.contextmanager
def _open_descriptor(path: Path) -> Iterator[tuple[int, int]]:
    """Open a regular file read-only for ffprobe's stdin.

    The descriptor is opened non-blocking so a FIFO cannot stall the open,
    and checked to be a regular file after opening.

    Args:
        path: The audio file.

    Yields:
        The descriptor and the file size.

    Raises:
        MediaError: If the file cannot be opened or is not a regular file.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MediaError(
            "cannot open audio file",
            operation="extract_audio_metadata",
            file_path=path,
            details={"error": str(exc)},
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise MediaError(
                "audio path is not a regular file",
                operation="extract_audio_metadata",
                file_path=path,
            )
        yield descriptor, info.st_size
    finally:
        os.close(descriptor)


def _rewind(descriptor: int) -> None:
    """Move a descriptor back to the start of the file.

    The descriptor shares its offset with ffprobe's stdin, so each call must
    start from byte zero.

    Args:
        descriptor: Open file descriptor.
    """
    os.lseek(descriptor, 0, os.SEEK_SET)


def _identity(descriptor: int) -> FileIdentity:
    """Return the identity and change data of an open file (A6).

    Args:
        descriptor: Open file descriptor.

    Returns:
        Device, inode, size, and modification time in nanoseconds.
    """
    info = os.fstat(descriptor)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _path_identity(path: Path) -> FileIdentity:
    """Return the identity and change data of the file a path names now.

    Args:
        path: The audio file's path.

    Returns:
        Device, inode, size, and modification time, or ``(-1,)`` when the
        path cannot be read, which never equals a real identity.
    """
    try:
        info = os.stat(path)
    except OSError:
        return (-1,)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _unavailable(reason: str) -> MediaMetadata:
    """Return all-unknown metadata for a missing backend (R10).

    Args:
        reason: Why ffprobe is unavailable.

    Returns:
        Metadata with only the flag and reason set.
    """
    return MediaMetadata(
        audio_flags=(AudioFlag.BACKEND_UNAVAILABLE,),
        audio_backend_reason=reason,
    )


__all__ = [
    "ACCEPTED_FORMAT_NAMES",
    "BENIGN_STDERR_BY_MAJOR",
    "BIT_DEPTH_CODECS",
    "DurationOutcome",
    "ESTIMATE_STDERR_LINES",
    "FORMAT_WHITELIST",
    "FileIdentity",
    "HEADER_STDOUT_LIMIT",
    "HEADER_TIMEOUT_SECONDS",
    "MATROSKA_FORMAT_NAME",
    "NO_DECLARED_DURATION_FORMATS",
    "PacketWalk",
    "ProbeHeader",
    "STDERR_LIMIT",
    "WALK_BASE_TIMEOUT_SECONDS",
    "WALK_BYTES_PER_EXTRA_SECOND",
    "WALK_MAX_TIMEOUT_SECONDS",
    "WALK_STDOUT_LIMIT",
    "benign_stderr_lines",
    "bit_depth_for",
    "decide_duration",
    "declared_duration",
    "extract_audio_metadata",
    "header_from_json",
    "interpret_header",
    "load_probe_json",
    "parse_decimal",
    "parse_positive_decimal",
    "parse_positive_int",
    "primary_audio_stream",
    "stderr_lines",
    "walk_timeout",
]
