"""Audio facts from guarded ffprobe.

ffprobe is the only source of audio facts: container, codec, sample rate,
channels, bit depth, bitrate, and duration. No Python code here reads a byte
of the media file. The file is opened once and handed to ffprobe as its
stdin: through ``-fd 0 fd:``, or, on an FFmpeg without the ``fd`` protocol,
through an extension-less symlink in a private directory that points at
``/dev/fd/0`` (at the file's absolute path where ``/dev/fd`` does not exist),
so ffprobe never sees the file's name. Anything ffprobe does not establish is
reported as unknown.

The rules are those of issue #400 and its amendments A1 to A14 (with A9b,
A10b, the revised A13a and the A14 rulings). This module runs ffprobe; the
decisions on its output live in :mod:`winnow.media._audio_rules`:

* R1/R2: one header call with a format allow-list that asks only for the
  fields the rules read, never tags (A12a); every fact is unknown unless
  the call exits 0, names an allowed container, has an audio stream with
  codec, sample rate, and channels, and writes only benign stderr lines.
  The header call never claims damage: an unexpected line, output that
  is not UTF-8 JSON, or JSON without the shape ffprobe writes is
  ``unrecognized_output``, a non-zero exit ``unsupported`` (A12b), clean
  output with no audio stream besides cover art (a video-only file; an
  empty M4A or MOV lists no stream) ``no_audio_stream`` (A14b), and an
  audio stream without a codec, sample rate, or channel count
  ``inconsistent``. Every result with all facts unknown carries at least
  one flag (A13c).
* A12c: ffprobe failing is not about the file. A signal or exit 126/127
  is ``backend_unavailable``; so is a ``-version`` banner that cannot be
  read or parsed, and then nothing is probed.
* R3: cover art is never a stream to read; the primary stream is the first
  default audio stream, else the first audio stream.
* R4/R5/R7: facts come from the primary stream; a declared duration is the
  stream's own ``duration``, or ``format.duration`` for a file with one
  non-picture stream; either only when the stream's first packet (and, for
  ``format.duration``, ``format.start_time``) is at most two time-base
  units after zero (A9); a Matroska ``DURATION`` tag is never used; AVI
  never declares a duration (A9b); in ASF nothing is declared when the
  file has another non-picture stream besides the primary audio (A8);
  AAC and MP3 in Matroska declare nothing because the duration counts the
  priming (A12h); in MP4 whose first packet is not before zero (no edit
  list), a codec other than PCM and the lossless ones (FLAC, ALAC,
  WavPack, TTA, TrueHD, MLP) keeps its declared duration only when it is
  above the packet span by more than the A5 tolerance, and otherwise
  declares nothing, because the packets hold the priming (A14a); in
  Matroska and MP4, a declared duration that equals the packet span of a
  stream starting before zero and runs past the last packet's end, each
  within the A5 tolerance, counts the priming and declares nothing (A13a
  revised); bitrate is the stream's own.
* R6: a packet walk of the primary stream checks the declared duration and
  measures it where none is declared. A walk that ends more than max(5 ms,
  two time-base units of the stream) before the declared duration is
  truncated (A5). In MP4 a packet longer than twice the median packet
  makes the duration unknown with ``inconsistent`` (A10b). In FLV a packet
  without a time makes every audio fact unknown with ``inconsistent``
  (A11). A walk with no packet is ``truncated`` only against a positive
  declared duration (A12d); a negative packet duration is a missing time,
  and a measured span or median that is not positive is ``inconsistent``
  (A12e). ``corrupt`` is set only here: for a non-zero walk exit after an
  accepted header, or a packet flagged corrupt with no stderr line off the
  benign list. On FFmpeg 9.0.2 a packet flagged corrupt always comes with a
  ``Packet corrupt`` stderr line, which is not benign, so that walk is
  ``unrecognized_output`` instead (A13d). A cut file gets ``truncated``,
  ``unrecognized_output``, ``unsupported``, ``estimated`` or
  ``inconsistent`` (some cuts of fragmented MP4: AAC cut to 35 or 40 %,
  Opus to 20 %, Vorbis to 55 % in the fixtures), or ``truncated=false``
  when the cut is no longer than the A5
  tolerance (A14c); no fixture or cut of one reaches ``corrupt`` on 9.0.2.
* ``truncated`` and ``corrupt`` claim damage; ``inconsistent``,
  ``estimated``, ``unrecognized_output``, ``unsupported`` and
  ``no_audio_stream`` only say the evidence does not establish a value,
  and a healthy file may carry them.
* R8: both calls are bounded in time and output and are never retried.
* A6: the file's device, inode, size, and modification time are taken before
  the header call and after the walk; any change makes every fact unknown
  with ``inconsistent``. Where ffprobe reads the file through its path,
  the path must also name the opened file before and after (A12g).
* A12f: a ``probe_score`` outside 0 to 100 is unknown; no value from
  ffprobe can fail the metadata model's validation.

Known limits (not worked around):

* Damage inside an intact packet is not detected; that needs a full decode.
* A damaged header byte can change a declared value (channels, sample rate,
  codec variant, duration) and leave no evidence; ``truncated=false`` means
  only that the audio present matches the declared length.
* A walk that ends no more than max(5 ms, two time-base units of the
  stream) before the declared duration counts as complete (A5). Where a
  codec pads its last frames (MP3, AAC), the padding hides more. Measured on
  FFmpeg 9.0.2 with 99 MP3 files (9 sample rates, 11 CBR and VBR settings)
  and 36 faststart AAC M4A files: one removed frame is never detected; with
  two removed, 52 MP3 and 14 M4A still report ``truncated=false``; with
  three, 5 MP3 and no M4A. The other cut MP3 files are ``truncated`` or
  ``unrecognized_output``. Wherever the duration is checked, a cut no
  longer than the tolerance is ``truncated=false`` with the declared
  duration (A14c).
* A Matroska ``DURATION`` tag is never used (A5). Matroska audio without
  its own stream duration is ``estimated`` unless it is the file's only
  non-picture stream and the file starts at zero.
* Audio whose first packet comes more than two time-base units after zero
  is ``estimated`` in every container (A9): its duration field can be the
  end of the file's timeline. Cuts are then not detected.
* AVI audio is always ``estimated`` (A9b): its duration field includes a
  start delay while the packets start at zero. ASF (WMA, WMV) audio is
  ``estimated`` when the file has any other non-picture stream (A8): the
  field is the whole file's play length.
* MP4 audio with a packet longer than twice the median packet has no
  duration and is ``inconsistent`` (A10b); fragmented MP4 stretches its
  first packet across a start delay. On 256 healthy generated MP4, M4A and
  MOV files the largest packet was 1.0 times the median. Other containers
  are not checked this way, so a stretched packet there goes unnoticed.
* AAC 5.1 in FLV gives an untimed configuration packet; every audio fact is
  then unknown with ``inconsistent`` (A11).
* Stereo PCM in Matroska can come back with the duration unknown and
  ``inconsistent``: ffprobe gives the last packet no duration.
* ADTS at 8 kHz is ``estimated`` up to about 170 ms long.
* AAC and MP3 in Matroska are ``estimated`` (A12h): the duration counts
  the encoder priming as audio. In MP4 whose first packet starts at or
  after zero (no edit list: fragmented MP4, or ``-use_editlist 0``), the
  packets hold the priming, so every codec but PCM and the lossless ones
  is ``estimated`` unless its declared duration is above the packet span
  by more than the A5 tolerance (A14a). A healthy file there declares the
  span or less; a cut shortens the span, so the declared duration stands
  and the cut is ``truncated``. Measured on FFmpeg 9.0.2, the excess of
  the measured length over the true length is one priming block at every
  sample rate: AAC 1024 samples (128 ms at 8 kHz, 92.9 ms at 11.025 kHz,
  85.3 ms at 12 kHz, 64 ms at 16 kHz, 46.4 ms at 22.05 kHz, 42.7 ms at
  24 kHz, 32 ms at 32 kHz, 23.2 ms at 44.1 kHz, 21.3 ms at 48 kHz), the
  same in Matroska and MP4; LAME MP3 in Matroska 1105 samples (138, 100,
  92, 69, 50, 46, 35, 25 and 23 ms); LAME MP3 in fragmented MP4 69 to
  104 ms at 16 kHz, 50 ms at 22.05 kHz, 46 ms at 24 kHz, 34.5 ms at
  32 kHz, 25 ms at 44.1 kHz and 23 ms at 48 kHz, and with
  ``-use_editlist 0`` 32 to 104 ms (ffmpeg writes no MP3 below 16 kHz
  into MP4); Vorbis in MP4 128 to 134 ms at 8 kHz down to 17 to 21 ms at
  48 kHz; Opus in MP4 6.5 ms; MP2 in MP4 8 to 70 ms; AC-3 and E-AC-3
  with ``-use_editlist 0`` 6 to 44 ms; AAC from Apple's AudioToolbox
  encoder (``aac_at``) 47 to 324 ms. The measured length still includes
  it.
* MP3 with ``-use_editlist 0`` at 24 to 48 kHz: the healthy packets run
  about one packet past the declared duration, so a cut of exactly one
  packet looks like a healthy file and is ``estimated`` (8 of 2,257
  packet-boundary cuts on FFmpeg 9.0.2; A14 rulings).
* Other primed audio in Matroska and MP4 is ``estimated`` when its
  declared duration is the packet span of a stream that starts before
  zero (A13a revised). Measured on FFmpeg 9.0.2, the declared duration
  was this far over the true length: fragmented AAC with ``delay_moov``,
  the AAC figures above; Vorbis in Matroska 128 to 134 ms at 8 kHz down to
  21 ms at 48 kHz; WMA in Matroska 6 to 60 ms; MP2 in Matroska 8 to 30 ms
  at 16 kHz down to 8 to 10 ms at 48 kHz; AC-3 and E-AC-3 in Matroska
  8 ms at 32 kHz and 6 ms at 44.1 kHz; Opus in Matroska 8 ms (its
  pre-skip). AC-3 and E-AC-3 in Matroska at 48 kHz are 5 ms over, inside
  the A5 tolerance, so they stay checked. A cut shortens the span but not
  the declared duration, so a cut file is never taken for priming.
* A checked duration is established to the precision of one codec frame:
  the packets themselves can run past the true length and ffprobe cannot
  tell. On 2,224 healthy generated files every checked duration was
  within one median packet of the true length, plus 1 ms in Matroska,
  whose timestamps are whole milliseconds; these combinations were above
  the A5 tolerance: WMA in audio-only ASF up to 60 ms (35 files; 24 to
  60 ms at 8 and 32 kHz, 18 to 39 ms at 48 kHz, 6 to 28 ms at the other
  rates); AAC from ``aac_at`` in faststart MP4 or MOV 6 to 80.7 ms, up to
  one frame (43 files); FLAC and WavPack in Matroska at 44.1 kHz 23 ms
  (2 files each); DTS in Matroska at 44.1 kHz 12 ms against an 11 ms
  median packet (2 files); AC-3 and E-AC-3 in fragmented MP4 with
  ``delay_moov`` 8 ms at 32 kHz, 5.8 ms at 44.1 kHz and 5.3 ms at 48 kHz
  (13 files each).
* MP3 is ``estimated`` only when ffprobe says it estimated the duration (no
  Xing/Info or VBRI header); with such a header the duration is declared and
  checked like any other.
* A cut exactly on an Ogg page, MPEG-TS packet, or raw ADTS/AC-3 frame
  boundary leaves no evidence; those formats are always ``estimated``, never
  ``truncated=false``.
* Chained Ogg is reported ``inconsistent``.
* A WAV or AIFF written to a pipe has no sizes in its header; neither
  makes a damage claim. The AIFF header does not parse: every fact is
  unknown with ``unsupported``. The WAV is read as data to the end of
  the file. When the data does not end on a 4096-byte packet, ffprobe
  prints ``Packet corrupt`` for the short last packet: in a short WAV
  (1 s at 8 kHz) the header call reads that far and every fact is unknown
  with ``unrecognized_output``; a long one (10 s) keeps its stream facts,
  with the duration unknown and ``unrecognized_output``. A piped WAV
  whose data is a multiple of 4096 bytes is ``estimated`` with the right
  duration. An MS ADPCM WAV whose ``fact`` chunk ffprobe ignores is
  ``estimated``.
* An AIFF with an ``APPL`` chunk signed ``stoc`` gives zero packets with a
  declared duration on FFmpeg 9.0.2, so it is reported ``truncated=true``
  although the file plays (A13e). This is an FFmpeg parse slip on a rare
  signature.
* Formats outside the allow-list (AU, Core Audio Format, raw DTS, VOC,
  W64, MLP, ADX) are ``unsupported``.
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
    HEADER_SHOW_ENTRIES,
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
        path_matches: Reports whether the path ffprobe reads names the
            opened file (A12g); always ``True`` when ffprobe reads the
            descriptor.
    """

    protocol: str
    input_args: tuple[str, ...]
    stdin: int
    size: int
    rewind: Callable[[], None]
    identity: Callable[[], FileIdentity]
    path_matches: Callable[[], bool] = lambda: True


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
    major = parse_version_major(ffprobe_version(binary) or "")
    if major is None:
        return _unavailable("ffprobe -version could not be read or parsed")
    benign = BENIGN_STDERR_BY_MAJOR.get(major, frozenset())
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

    Where ffprobe reads the file through its path, the path must name the
    opened file both before and after the probe (A12g); a mismatch, or a
    path that no longer exists, is ``inconsistent``.

    Args:
        binary: Absolute path of the ffprobe binary.
        source: How ffprobe receives the file.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The audio metadata, or all unknown with ``inconsistent`` when the
        file's identity differs after the last call from before the first,
        or the path does not name the opened file.
    """
    changed = MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,))
    if not source.path_matches():
        return changed
    before = source.identity()
    metadata = _probe(binary=binary, source=source, benign=benign)
    if source.identity() != before or not source.path_matches():
        return changed
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
            "-show_entries",
            HEADER_SHOW_ENTRIES,
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
    if outcome.facts_unknown:
        return MediaMetadata(audio_flags=outcome.flags)
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
    absolute path, and the path's identity must equal the descriptor's
    before and after the probe (A6, A12g).

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
                identity=lambda: _identity(descriptor),
                path_matches=(
                    (lambda: True)
                    if by_descriptor
                    else (lambda: _path_identity(path) == _identity(descriptor))
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
