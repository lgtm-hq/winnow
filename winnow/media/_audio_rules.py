"""Pure rules of the guarded ffprobe audio reader (issue #400).

Everything here works on ffprobe output that has already been captured:
stderr filtering against the benign list (R2 with amendments A1 to A3),
strict JSON parsing (R1), the header outcome (R2 with A12a to A12c), stream
selection and facts (R2 to R4, A12f), the declared duration (R5 with A5, A8,
A9, A9b and A15b), and the packet-walk decision (R6 with A5, A10b, A11,
A12d, A12e and A15a).
Nothing here starts a process or touches a file; :mod:`winnow.media.audio`
runs ffprobe and feeds these functions.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from winnow.media._ffprobe import BoundedRun
from winnow.models.media import AudioFlag

ACCEPTED_FORMAT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "mp3",
        "aac",
        "ac3",
        "eac3",
        "flac",
        "wv",
        "tta",
        "wav",
        "aiff",
        "mov,mp4,m4a,3gp,3g2,mj2",
        "ogg",
        "asf",
        "matroska,webm",
        "avi",
        "mpegts",
        "mpeg",
        "flv",
    },
)
"""Exact ``format_name`` values whose facts are accepted (R2)."""


NO_DECLARED_DURATION_FORMATS: Final[frozenset[str]] = frozenset(
    {"ogg", "mpegts", "mpeg", "flv", "aac", "ac3", "eac3", "avi"},
)
"""Containers whose duration is never taken as declared (R5, A9b).

AVI is here because its audio duration field includes a start delay while
the packet times start at zero, with or without other streams (A9b).
"""


AUDIO_ONLY_DECLARED_FORMATS: Final[frozenset[str]] = frozenset({"asf"})
"""Containers whose audio duration is declared only without other streams (A8).

In ASF the audio stream's duration is the whole file's play length, set by
the video's end. Any non-picture stream besides the primary audio stream
makes the duration undeclared. AVI declares nothing at all (A9b).
"""


ESTIMATE_STDERR_LINES: Final[frozenset[str]] = frozenset(
    {
        "Estimating duration from bitrate, this may be inaccurate",
        "invalid concatenated file detected - using bitrate for duration",
    },
)
"""Benign lines that mean ffprobe estimated the duration (R2, R5)."""


DATA_TRACK_WARNING: Final[str] = "Unsupported codec with id <N> for input stream <M>"
"""Pin for ffprobe's warning about a track it has no decoder for (A2).

A line ``Unsupported codec with id N for input stream M`` is benign only when
the header JSON has a stream with index M whose ``codec_type`` is not
``audio``; for an audio stream, or an index the JSON does not have, the line
is unrecognized.
"""


COVER_TIMESCALE_WARNING: Final[str] = "stream 0, timescale not set"
"""Pin for the MP4 demuxer's warning about ``covr`` cover art (A3).

In FFmpeg 9.0.2, ``libavformat/mov.c`` ``mov_read_covr`` (lines 232 to 271)
makes a stream for each ``covr`` image with a zeroed stream context whose
``ffindex`` and ``time_scale`` are never set. ``mov_read_header`` then calls
``fix_timescale`` (line 11351) for every stream, and ``fix_timescale``
(lines 5242 to 5250) prints ``stream %d, timescale not set`` with that
``ffindex``, so each ``covr`` image prints exactly this line, naming stream 0
whatever its real index. A real track whose ``mdhd`` time scale is not set
prints the same text with its own index from ``mov_read_track`` (line 5351)
and gets the movie time scale, or 1, instead.

The line is benign only when the container is the MP4 family, the JSON has
an attached picture with ``id`` ``0x0`` (a ``covr`` image; tracks have a
non-zero track id), and stream 0 is the primary audio stream with
``time_base`` exactly ``1/<sample_rate>``, so a stream 0 that fell back to
another time scale is not accepted.
"""


BENIGN_STDERR_BY_MAJOR: Final[dict[int, frozenset[str]]] = {
    9: ESTIMATE_STDERR_LINES
    | frozenset(
        {
            "ignoring wrong sample_count",
            "Ignoring maximum wav data size, file may be invalid",
            DATA_TRACK_WARNING,
            COVER_TIMESCALE_WARNING,
        },
    ),
}
"""Stderr lines that do not invalidate a run, per FFmpeg major (R2, R11).

A major without an entry has no benign lines: any stderr line makes the facts
unknown. :data:`DATA_TRACK_WARNING` and :data:`COVER_TIMESCALE_WARNING` are
pins for lines that are benign only under their conditions.
"""


CONDITIONAL_STDERR_LINES: Final[frozenset[str]] = frozenset(
    {DATA_TRACK_WARNING, COVER_TIMESCALE_WARNING},
)
"""Pins that are never matched as plain text."""


MAX_DISTINCT_PACKET_DURATIONS: Final[int] = 4096
"""Distinct packet durations a walk keeps before it gives up (A7).

A walk with more distinct durations reports ``inconsistent``; healthy
fixtures have at most a handful.
"""


PROGRAM_FAILURE_EXIT_CODES: Final[frozenset[int]] = frozenset({126, 127})
"""Shell exit codes for a program that could not run (A12c).

126 is "found but not executable" and 127 "not found"; like a negative
return code (killed by a signal), they say nothing about the file.
"""


PROBE_SCORE_MAX: Final[int] = 100
"""Largest ``probe_score`` ffprobe can report; anything outside 0 to 100 is
unknown (A12f)."""


HEADER_SHOW_ENTRIES: Final[str] = (
    "format=format_name,probe_score,duration,start_time"
    ":stream=index,id,codec_type,codec_name,sample_rate,channels,"
    "bits_per_raw_sample,bits_per_sample,bit_rate,time_base,duration,"
    "nb_frames,profile"
    ":stream_disposition=default,attached_pic"
)
"""The only header fields the rules read (A12a); tags are never requested.

``nb_frames`` tells a fragmented MP4 apart (A15a); ``profile`` names
HE-AAC (A15b).
"""


BIT_DEPTH_CODECS: Final[frozenset[str]] = frozenset(
    {"flac", "alac", "wavpack", "tta", "ape", "mlp", "truehd", "wmalossless"},
)
"""Codecs besides ``pcm_*`` whose bit depth is reported (R4)."""


_LOG_PREFIX: Final[re.Pattern[str]] = re.compile(r"^(?:\[[^\]]* @ 0x[0-9a-fA-F]+\] ?)+")


_INT_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9]{1,18}")


_DECIMAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"-?[0-9]{1,15}(?:\.[0-9]{1,12})?",
)


_TIME_BASE: Final[re.Pattern[str]] = re.compile(
    r"(?P<num>[0-9]{1,10})/(?P<den>[0-9]{1,10})",
)


_DATA_TRACK_LINE: Final[re.Pattern[str]] = re.compile(
    r"Unsupported codec with id [0-9]{1,10} for input stream (?P<stream>[0-9]{1,6})",
)


MP4_FORMAT_NAME: Final[str] = "mov,mp4,m4a,3gp,3g2,mj2"


MATROSKA_FORMAT_NAME: Final[str] = "matroska,webm"


PRIMED_MATROSKA_CODECS: Final[frozenset[str]] = frozenset({"aac", "mp3"})
"""Codecs whose Matroska duration counts the encoder priming (A12h).

ffmpeg's Matroska muxer writes no codec delay for AAC or MP3, so the first
packet starts before zero by the priming (1024 samples for AAC, 1105 for
LAME MP3) and ``format.duration`` is measured from that negative start. The
declared length then overshoots the audio by one priming block at every
sample rate: 21 to 23 ms at 48 kHz, 128 to 138 ms at 8 kHz.
"""


PCM_CODEC_PREFIX: Final[str] = "pcm_"
"""Codec-name prefix of PCM, which has no encoder priming (A14a)."""


UNPRIMED_CODECS: Final[frozenset[str]] = frozenset(
    {"flac", "alac", "wavpack", "tta", "truehd", "mlp"},
)
"""Lossless codecs besides ``pcm_*`` with no encoder priming (A14a).

An MP4 muxer that writes an edit list hides the priming: the first packet
starts before zero and the stream duration is the audio's length. Without
an edit list (fragmented MP4, or ``-use_editlist 0``) the first packet
starts at zero and the packets hold the priming. Every codec but PCM and
these may then count its priming in the declared duration. Measured on
FFmpeg 9.0.2: AAC 1024 samples, LAME MP3 1105, Vorbis up to 134 ms, Opus
6.5 ms, MP2 about 10 ms, AC-3 and E-AC-3 5.3 to 8 ms.
"""


PRIMING_SPAN_FORMATS: Final[frozenset[str]] = frozenset(
    {MATROSKA_FORMAT_NAME, MP4_FORMAT_NAME},
)
"""Containers where a declared duration equal to the packet span of a
stream that starts before zero counts the priming (A13a revised).

The rule for a stream starting at or after zero applies to MP4 alone (A14a).
"""


_THREE: Final[Decimal] = Decimal(3)


SHORTFALL_FLOOR_SECONDS: Final[Decimal] = Decimal("0.005")
"""Smallest shortfall below the declared duration that counts (R6 step 6, A5)."""


SHORTFALL_TIME_BASE_UNITS: Final[Decimal] = Decimal(2)
"""Time-base units of the primary stream a shortfall must exceed (A5)."""


START_TIME_BASE_UNITS: Final[Decimal] = Decimal(2)
"""Time-base units after zero a stream may start and still declare (A9)."""


GAP_PACKET_MEDIAN_RATIO: Final[Decimal] = Decimal(2)
"""A packet longer than this many median packets is stretched (A10b)."""


GAP_PACKET_FORMATS: Final[frozenset[str]] = frozenset({MP4_FORMAT_NAME})
"""Containers where a stretched packet makes the duration unknown (A10b).

Packet durations in MP4 audio are uniform apart from a shorter last packet.
Other containers carry codecs such as Vorbis that vary packet size by design.
"""


HE_AAC_PROFILES: Final[frozenset[str]] = frozenset({"HE-AAC", "HE-AACv2"})
"""AAC ``profile`` values that ffprobe reports for HE-AAC (A15b).

HE-AAC and HE-AACv2 from AudioToolbox in MP4 declare a duration 89 to
376 ms past the true length, 2 to 3 packets, so a checked value would be
more than a codec frame long. The measured span that is reported instead,
``estimated``, is 133 to 508 ms past the true length: always over, and by
3 to 4 packets (43 ms packets at 48 kHz, 128 ms at 16 kHz). Measured on
FFmpeg 9.0.2 across 16 to 48 kHz, mono and stereo, 0.5 to 10 s (A15f-3).
"""


UNTIMED_PACKET_FORMATS: Final[frozenset[str]] = frozenset({"flv"})
"""Containers where an untimed packet makes every audio fact unknown (A11).

AAC 5.1 in FLV gives an untimed configuration packet, and for some source
rates ffprobe then reports a sample rate the file does not have.
"""


class _DuplicateKeyError(ValueError):
    """Raised when ffprobe JSON repeats a key inside one object."""


@dataclass(frozen=True, slots=True)
class StderrContext:
    """Facts from the header JSON that decide the conditional pins.

    Args:
        non_audio_indexes: Stream indexes whose ``codec_type`` is not audio.
        cover_timescale_benign: Whether :data:`COVER_TIMESCALE_WARNING`'s
            condition holds.
    """

    non_audio_indexes: frozenset[int] = frozenset()
    cover_timescale_benign: bool = False


@dataclass(frozen=True, slots=True)
class ProbeHeader:
    """Facts accepted from the header call (R2 to R5, R7).

    Args:
        container: ``format_name``.
        probe_score: ``probe_score`` when reported.
        stream_index: Index of the primary audio stream.
        codec: ``codec_name`` of the primary stream.
        sample_rate: Sample rate of the primary stream.
        channels: Channel count of the primary stream.
        bit_depth: Bit depth when R4 allows one.
        bitrate: The primary stream's own ``bit_rate``.
        audio_stream_count: Audio streams other than cover art.
        non_picture_stream_count: Streams of any type other than cover art.
        time_base: The primary stream's ``time_base`` in seconds.
        stream_duration: The primary stream's ``duration``.
        format_duration: ``format.duration``.
        format_start: ``format.start_time``.
        estimate_warned: ``True`` when stderr says the duration was estimated.
        stderr_context: Conditions for the conditional stderr pins, used for
            the walk's stderr too.
        frame_count: The primary stream's ``nb_frames``. In MP4 it counts
            only the samples indexed in the ``moov`` box, not those in
            fragments (A15a). An MP4 sample is a packet for most codecs
            but one audio sample for PCM and ADPCM (A15f-1).
        profile: The primary stream's ``profile`` (A15b).
    """

    container: str
    probe_score: int | None
    stream_index: int
    codec: str
    sample_rate: int
    channels: int
    bit_depth: int | None
    bitrate: int | None
    audio_stream_count: int
    non_picture_stream_count: int
    time_base: Decimal | None
    stream_duration: Decimal | None
    format_duration: Decimal | None
    format_start: Decimal | None
    estimate_warned: bool
    stderr_context: StderrContext = StderrContext()
    frame_count: int | None = None
    profile: str | None = None


@dataclass(slots=True)
class PacketWalk:
    """Running summary of a packet walk, fed one CSV line at a time (R6).

    Only counts, a few times, and at most
    :data:`MAX_DISTINCT_PACKET_DURATIONS` distinct durations are kept, so the
    memory a walk needs is bounded whatever the file holds.
    """

    packets: int = 0
    first_time: Decimal | None = None
    last_time: Decimal | None = None
    last_duration: Decimal | None = None
    first_duration: Decimal | None = None
    missing_time: bool = False
    backwards: bool = False
    corrupt_packet: bool = False
    too_varied: bool = False
    longest_duration: Decimal | None = None
    durations: Counter[Decimal] = field(default_factory=Counter)

    def feed(self, raw: bytes) -> None:
        """Account for one ``pts_time,duration_time,flags`` line.

        Args:
            raw: One stdout line without its newline.
        """
        line = raw.rstrip(b"\r")
        if not line:
            return
        self.packets += 1
        try:
            fields = line.decode("ascii").split(",")
        except UnicodeDecodeError:
            self.missing_time = True
            return
        if len(fields) < 3:
            self.missing_time = True
            return
        if "C" in fields[2]:
            self.corrupt_packet = True
        time = parse_decimal(fields[0])
        duration = parse_decimal(fields[1])
        if time is None or duration is None or duration < 0:
            self.missing_time = True
            return
        if self.last_time is not None and time < self.last_time:
            self.backwards = True
        if self.first_time is None:
            self.first_time = time
            self.first_duration = duration
        self.last_time = time
        self.last_duration = duration
        if self.longest_duration is None or duration > self.longest_duration:
            self.longest_duration = duration
        if (
            duration not in self.durations
            and len(self.durations) >= MAX_DISTINCT_PACKET_DURATIONS
        ):
            self.too_varied = True
            return
        self.durations[duration] += 1

    def median_duration(self) -> Decimal | None:
        """Return the lower median packet duration.

        Returns:
            The median, or ``None`` when no packet had a duration.
        """
        count = sum(self.durations.values())
        if count == 0:
            return None
        target = (count - 1) // 2
        ordered = sorted(self.durations)
        seen = 0
        for value in ordered:
            seen += self.durations[value]
            if seen > target:
                return value
        return ordered[-1]

    def measured_duration(self) -> Decimal | None:
        """Return last time + last duration - first time.

        Returns:
            The measured duration, or ``None`` without timed packets.
        """
        if (
            self.first_time is None
            or self.last_time is None
            or self.last_duration is None
        ):
            return None
        return self.last_time + self.last_duration - self.first_time


@dataclass(frozen=True, slots=True)
class DurationOutcome:
    """Duration, truncation state, and flags decided by R6.

    Args:
        duration: Reported duration in seconds, or ``None`` when unknown.
        truncated: ``True``/``False`` when checked, ``None`` when not.
        flags: Flags raised.
        facts_unknown: ``True`` when the walk makes every audio fact from
            the header unknown too, not only the duration (A11).
    """

    duration: Decimal | None
    truncated: bool | None
    flags: tuple[AudioFlag, ...] = ()
    facts_unknown: bool = False


def stderr_lines(raw: bytes) -> list[str]:
    """Split ffprobe stderr into lines that count for R2.

    The ``[name @ 0x...]`` prefix is removed and empty lines and "Last
    message repeated" lines are dropped.

    Args:
        raw: Captured stderr.

    Returns:
        Remaining lines, stripped.
    """
    lines: list[str] = []
    for raw_line in raw.decode("utf-8", errors="replace").splitlines():
        line = _LOG_PREFIX.sub("", raw_line).strip()
        if not line or line.startswith("Last message repeated"):
            continue
        lines.append(line)
    return lines


def interpret_header(
    run: BoundedRun,
    *,
    benign: frozenset[str],
) -> tuple[ProbeHeader | None, tuple[AudioFlag, ...]]:
    """Apply R2 to R5 and R7 to a finished header call.

    The header call never claims damage. A signal or exit 126 or 127 is
    ``backend_unavailable`` (A12c); any other non-zero exit means no accepted
    format recognised the file, ``unsupported`` (A12b). Stdout that is not
    valid UTF-8 or JSON, and a stderr line off the benign list, are
    ``unrecognized_output`` (A1, A12a). When clean output gives no
    accepted header, every fact is unknown, so a flag must say why (A13c);
    :func:`header_rejection` picks it (A14b).

    Args:
        run: The header call; it must not have breached a bound.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The accepted header, or ``None`` with at least one flag saying why not.
    """
    if program_failed(run.returncode):
        return None, (AudioFlag.BACKEND_UNAVAILABLE,)
    if run.returncode != 0:
        return None, (AudioFlag.UNSUPPORTED,)
    data = load_probe_json(run.stdout)
    if data is None:
        return None, (AudioFlag.UNRECOGNIZED_OUTPUT,)
    lines = stderr_lines(run.stderr)
    context = stderr_context(data)
    if has_unrecognized_line(lines, benign=benign, context=context):
        return None, (AudioFlag.UNRECOGNIZED_OUTPUT,)
    estimate_warned = any(line in ESTIMATE_STDERR_LINES for line in lines)
    header = header_from_json(data, estimate_warned=estimate_warned)
    if header is None:
        return None, (header_rejection(data),)
    return replace(header, stderr_context=context), ()


def header_rejection(data: dict[str, object]) -> AudioFlag:
    """Say why clean header JSON gave no accepted header (A13c, A14b).

    Args:
        data: Parsed header JSON that :func:`header_from_json` rejected.

    Returns:
        ``unrecognized_output`` when the JSON does not have the shape
        ffprobe writes (a missing ``format`` object or ``streams`` list, a
        missing or non-string ``format_name``, a stream that is not an
        object, or an audio stream index that is not a non-negative
        integer); ``no_audio_stream`` when the streams hold
        no audio stream besides cover art, as in a video-only file or an
        empty M4A or MOV; ``inconsistent`` otherwise, as for a container
        off the allow-list or an audio stream without a codec, sample rate,
        or channel count.
    """
    container_section = data.get("format")
    streams = data.get("streams")
    if (
        not isinstance(container_section, dict)
        or not isinstance(streams, list)
        or not isinstance(container_section.get("format_name"), str)
        or not all(isinstance(stream, dict) for stream in streams)
    ):
        return AudioFlag.UNRECOGNIZED_OUTPUT
    if _accepted_sections(data) is None:
        return AudioFlag.INCONSISTENT
    audio = [
        stream
        for stream in streams
        if not _is_attached_pic(stream) and stream.get("codec_type") == "audio"
    ]
    if not audio:
        return AudioFlag.NO_AUDIO_STREAM
    if primary_audio_stream(audio) is None:
        return AudioFlag.UNRECOGNIZED_OUTPUT
    return AudioFlag.INCONSISTENT


def program_failed(returncode: int) -> bool:
    """Report whether an exit status says ffprobe itself failed (A12c).

    Args:
        returncode: Exit status after reaping.

    Returns:
        ``True`` for a signal (negative status) or exit 126 or 127.
    """
    return returncode < 0 or returncode in PROGRAM_FAILURE_EXIT_CODES


def has_unrecognized_line(
    lines: list[str],
    *,
    benign: frozenset[str],
    context: StderrContext,
) -> bool:
    """Report whether any stderr line is off the benign list (R2, A1 to A3).

    Args:
        lines: Stderr lines after :func:`stderr_lines`.
        benign: Benign stderr lines for the installed FFmpeg major.
        context: Conditions for the conditional pins.

    Returns:
        ``True`` when at least one line is not benign for this file.
    """
    return not all(_line_is_benign(line, benign, context) for line in lines)


def _line_is_benign(line: str, benign: frozenset[str], context: StderrContext) -> bool:
    """Decide one stderr line.

    Args:
        line: A stderr line after :func:`stderr_lines`.
        benign: Benign stderr lines for the installed FFmpeg major.
        context: Conditions for the conditional pins.

    Returns:
        ``True`` when the line is benign for this file.
    """
    if line in benign and line not in CONDITIONAL_STDERR_LINES:
        return True
    if line == COVER_TIMESCALE_WARNING:
        return COVER_TIMESCALE_WARNING in benign and context.cover_timescale_benign
    match = _DATA_TRACK_LINE.fullmatch(line)
    if match is None or DATA_TRACK_WARNING not in benign:
        return False
    return int(match.group("stream")) in context.non_audio_indexes


def stderr_context(data: dict[str, object]) -> StderrContext:
    """Collect the header JSON facts the conditional pins depend on.

    Args:
        data: Parsed header JSON.

    Returns:
        The context; empty when the JSON does not have the expected shape.
    """
    sections = _accepted_sections(data)
    if sections is None:
        return StderrContext()
    _container_section, container, streams = sections
    non_audio: set[int] = set()
    for stream in streams:
        index = stream.get("index")
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and isinstance(stream.get("codec_type"), str)
            and stream.get("codec_type") != "audio"
        ):
            non_audio.add(index)
    return StderrContext(
        non_audio_indexes=frozenset(non_audio),
        cover_timescale_benign=_cover_timescale_benign(container, streams),
    )


def _cover_timescale_benign(container: str, streams: list[dict[str, object]]) -> bool:
    """Check the condition of :data:`COVER_TIMESCALE_WARNING` (A3).

    Args:
        container: ``format_name``.
        streams: Stream objects of the header JSON.

    Returns:
        ``True`` for an MP4-family file with ``covr`` cover art whose stream
        0 is the primary audio stream with time base ``1/<sample_rate>``.
    """
    if container != MP4_FORMAT_NAME:
        return False
    if not any(_is_attached_pic(s) and s.get("id") == "0x0" for s in streams):
        return False
    audio = [
        stream
        for stream in streams
        if not _is_attached_pic(stream) and stream.get("codec_type") == "audio"
    ]
    primary = primary_audio_stream(audio)
    if primary is None or primary.get("index") != 0:
        return False
    sample_rate = parse_positive_int(primary.get("sample_rate"))
    return sample_rate is not None and primary.get("time_base") == f"1/{sample_rate}"


def load_probe_json(payload: bytes) -> dict[str, object] | None:
    """Parse ffprobe JSON strictly.

    The payload must be valid UTF-8 and valid JSON without repeated keys in
    any object or non-finite number constants.

    Args:
        payload: Raw stdout of the header call.

    Returns:
        The top-level object, or ``None`` when the payload fails any check.
    """
    try:
        text = payload.decode("utf-8", errors="strict")
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def header_from_json(
    data: dict[str, object],
    *,
    estimate_warned: bool,
) -> ProbeHeader | None:
    """Build the accepted header facts from ffprobe JSON (R2 to R5, R7).

    Args:
        data: Parsed header JSON.
        estimate_warned: Whether stderr carried an estimate line.

    Returns:
        The header, or ``None`` when R2's acceptance conditions fail.
    """
    sections = _accepted_sections(data)
    if sections is None:
        return None
    container_section, container, streams = sections
    non_picture = [stream for stream in streams if not _is_attached_pic(stream)]
    audio = [stream for stream in non_picture if stream.get("codec_type") == "audio"]
    primary = primary_audio_stream(audio)
    facts = _primary_facts(primary) if primary is not None else None
    if primary is None or facts is None:
        return None
    index, codec, sample_rate, channels = facts
    return ProbeHeader(
        container=container,
        probe_score=parse_probe_score(container_section.get("probe_score")),
        stream_index=index,
        codec=codec,
        sample_rate=sample_rate,
        channels=channels,
        bit_depth=bit_depth_for(primary, codec=codec),
        bitrate=parse_positive_int(primary.get("bit_rate")),
        audio_stream_count=len(audio),
        non_picture_stream_count=len(non_picture),
        time_base=parse_time_base(primary.get("time_base")),
        stream_duration=parse_positive_decimal(primary.get("duration")),
        format_duration=parse_positive_decimal(container_section.get("duration")),
        format_start=parse_decimal(container_section.get("start_time")),
        estimate_warned=estimate_warned,
        frame_count=parse_positive_int(primary.get("nb_frames")),
        profile=_text(primary.get("profile")),
    )


def _text(value: object) -> str | None:
    """Return a non-empty string value.

    Args:
        value: Raw ffprobe value.

    Returns:
        The string, or ``None`` for anything else.
    """
    return value if isinstance(value, str) and value else None


def _accepted_sections(
    data: dict[str, object],
) -> tuple[dict[str, object], str, list[dict[str, object]]] | None:
    """Return the format section, container, and streams when well formed.

    Args:
        data: Parsed header JSON.

    Returns:
        The ``format`` object, its allowed ``format_name``, and the stream
        objects; ``None`` when the shape is wrong or the container is not on
        the R2 list.
    """
    container_section = data.get("format")
    streams = data.get("streams")
    if not isinstance(container_section, dict) or not isinstance(streams, list):
        return None
    container = container_section.get("format_name")
    if not isinstance(container, str) or container not in ACCEPTED_FORMAT_NAMES:
        return None
    if not all(isinstance(stream, dict) for stream in streams):
        return None
    return container_section, container, streams


def _primary_facts(stream: dict[str, object]) -> tuple[int, str, int, int] | None:
    """Return the facts R2 requires of the primary stream.

    Args:
        stream: The primary audio stream.

    Returns:
        Index, codec, sample rate, and channels, or ``None`` when any is
        missing or not positive.
    """
    index = stream.get("index")
    codec = stream.get("codec_name")
    sample_rate = parse_positive_int(stream.get("sample_rate"))
    channels = parse_positive_int(stream.get("channels"))
    if not isinstance(index, int) or not isinstance(codec, str) or not codec:
        return None
    if sample_rate is None or channels is None:
        return None
    return index, codec, sample_rate, channels


def primary_audio_stream(audio: list[dict[str, object]]) -> dict[str, object] | None:
    """Choose the primary audio stream (R3).

    Args:
        audio: Audio streams with cover art already removed.

    Returns:
        The lowest-index default stream, else the lowest-index stream, or
        ``None`` when there is no audio stream or an index is not an integer.
    """
    indexed: list[tuple[int, dict[str, object]]] = []
    for stream in audio:
        index = stream.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return None
        indexed.append((index, stream))
    indexed.sort(key=lambda pair: pair[0])
    for _index, stream in indexed:
        if _disposition(stream, "default"):
            return stream
    return indexed[0][1] if indexed else None


def bit_depth_for(stream: dict[str, object], *, codec: str) -> int | None:
    """Return the bit depth R4 allows for a stream.

    Args:
        stream: The primary audio stream.
        codec: Its codec name.

    Returns:
        ``bits_per_raw_sample``, else ``bits_per_sample``, for PCM and the
        listed lossless codecs; ``None`` otherwise or when both are zero or
        ``N/A``.
    """
    if not codec.startswith(PCM_CODEC_PREFIX) and codec not in BIT_DEPTH_CODECS:
        return None
    raw = parse_positive_int(stream.get("bits_per_raw_sample"))
    if raw is not None:
        return raw
    return parse_positive_int(stream.get("bits_per_sample"))


def declared_duration(
    header: ProbeHeader,
    walk: PacketWalk,
    *,
    estimate_warned: bool,
) -> Decimal | None:
    """Return the declared duration R5 allows, if any.

    Nothing is declared unless the primary stream's first packet time is at
    most two time-base units after zero; negative first times from priming
    or edit lists are allowed (A9). In AVI, ASF and Matroska a stream's
    duration field can be the end time of the file's timeline rather than
    the audio's length, so a stream that starts late declares nothing. AVI
    never declares a duration (A9b). AAC and MP3 in Matroska declare
    nothing: their duration counts the encoder priming (A12h). In MP4, a
    stream of any codec but PCM and the lossless ones whose first packet
    starts at or after zero declares nothing unless its declared duration
    is above the packet span by more than the A5 tolerance (A14a). In
    Matroska and MP4, a declared duration that equals the packet span of a
    stream starting before zero, and runs past the last packet's end,
    counts the priming too and declares nothing (A13a revised). In MP4,
    HE-AAC declares nothing, and neither does a stream whose first packet is
    longer than the median packet by more than the A5 tolerance: the packet
    is stretched across a start delay, so the span counts the delay (A15b).

    The stream's own ``duration`` is declared. Otherwise ``format.duration``
    is declared only for a file with one non-picture stream whose
    ``format.start_time`` is within the same bound (A9). A Matroska
    ``DURATION`` tag is never used (A5): for audio that starts late, the tag
    minus the lead overshoots the real end by the codec's priming. In ASF
    nothing is declared when the file has any non-picture stream besides the
    primary audio stream (A8).

    Args:
        header: Accepted header facts.
        walk: Completed packet walk of the primary stream.
        estimate_warned: Whether either call's stderr carried an estimate line.

    Returns:
        The declared duration, or ``None`` when nothing declares one.
    """
    if not _starts_at_zero(header, walk.first_time):
        return None
    if header.container == MP4_FORMAT_NAME and (
        is_he_aac(header) or first_packet_stretched(header, walk)
    ):
        return None
    declared = declared_without_packets(header, estimate_warned=estimate_warned)
    if declared is None or _declared_counts_priming(header, walk, declared):
        return None
    return declared


def _declared_counts_priming(
    header: ProbeHeader,
    walk: PacketWalk,
    declared: Decimal,
) -> bool:
    """Report whether a declared duration may count the priming as audio.

    The span is the last packet's end minus the first packet's start.

    * First packet at or after zero, in MP4 (A14a): with no edit list to
      trim it (fragmented MP4, ``-use_editlist 0``) the packets hold the
      priming. For every codec but PCM and :data:`UNPRIMED_CODECS`, the
      declared duration is kept only when it is above the span by more than
      the A5 tolerance; the truncation check then applies, so a cut stays
      ``truncated``. Otherwise, including a declared duration below the
      span (MP3 and MP2 with ``-use_editlist 0``), nothing is declared.
    * First packet before zero, in Matroska and MP4 (A13a revised): the
      declared duration equals the span and exceeds the last packet's end,
      each within the A5 tolerance. A cut shortens the span but not the
      declared duration, so a cut file never matches and keeps its
      ``truncated`` result. An edit list that trims the priming (faststart
      AAC) makes the declared duration shorter than the span, so that file
      does not match either.

    Args:
        header: Accepted header facts.
        walk: Completed packet walk of the primary stream.
        declared: The declared duration R5 allows otherwise.

    Returns:
        ``True`` when the rule for the stream's layout holds.
    """
    if header.container not in PRIMING_SPAN_FORMATS:
        return False
    span = walk.measured_duration()
    first = walk.first_time
    if span is None or first is None:
        return False
    tolerance = shortfall_tolerance(header)
    if first >= 0:
        return (
            header.container == MP4_FORMAT_NAME
            and may_be_primed(header.codec)
            and declared - span <= tolerance
        )
    last_end = span + first
    return abs(declared - span) <= tolerance and declared - last_end > tolerance


def is_he_aac(header: ProbeHeader) -> bool:
    """Report whether the primary stream is HE-AAC (A15b).

    Args:
        header: Accepted header facts.

    Returns:
        ``True`` for AAC whose ``profile`` is in :data:`HE_AAC_PROFILES`.
    """
    return header.codec == "aac" and header.profile in HE_AAC_PROFILES


def first_packet_stretched(header: ProbeHeader, walk: PacketWalk) -> bool:
    """Report whether the first packet is longer than the median (A15b).

    Without an edit list, an MP4 muxer stretches the first audio packet
    across the start delay that B-frame video gives the file (40 ms at
    25 fps for one B-frame). That stays under the A10b bound when the
    delay is shorter than a packet.

    Args:
        header: Accepted header facts.
        walk: Completed packet walk of the primary stream.

    Returns:
        ``True`` when the first packet exceeds the median packet duration
        by more than the A5 tolerance.
    """
    first = walk.first_duration
    median = walk.median_duration()
    if first is None or median is None:
        return False
    return first - median > shortfall_tolerance(header)


def is_fragmented(header: ProbeHeader, walk: PacketWalk) -> bool:
    """Report whether an MP4 file holds packets outside its ``moov`` index (A15a).

    ``nb_frames`` counts the samples the ``moov`` box indexes; samples in
    movie fragments (``moof``) are not counted. A walk that finds more
    packets than that read fragments. ffprobe reports no field that tells
    a fragment index (``sidx`` or ``mfra``) apart, so every fragmented MP4
    is treated as unindexed.

    For PCM and ADPCM an MP4 sample is one audio sample, so ``nb_frames``
    exceeds the packet count and a packet comparison never finds the
    fragments. When ``nb_frames`` exceeds the packet count, the comparison
    is made in audio samples instead (A15f-1): the measured span times the
    sample rate, rounded, against ``nb_frames``, with the A5 tolerance
    converted to samples. The rule keys on that count mismatch, not on the
    codec.

    Args:
        header: Accepted header facts.
        walk: Completed packet walk of the primary stream.

    Returns:
        ``True`` for MP4 whose walk found more packets than ``nb_frames``
        (taken as zero when ffprobe does not report it), or, when
        ``nb_frames`` exceeds the packet count, whose measured span holds
        more samples than ``nb_frames`` by more than the tolerance.
    """
    if header.container != MP4_FORMAT_NAME:
        return False
    indexed = header.frame_count or 0
    if indexed <= walk.packets:
        return walk.packets > indexed
    measured = walk.measured_duration()
    if measured is None:
        return False
    rate = Decimal(header.sample_rate)
    span_samples = (measured * rate).to_integral_value(rounding=ROUND_HALF_EVEN)
    return span_samples - indexed > shortfall_tolerance(header) * rate


def may_be_primed(codec: str) -> bool:
    """Report whether a codec may carry encoder priming (A14a).

    Args:
        codec: Codec name of the primary stream.

    Returns:
        ``False`` for PCM and the codecs in :data:`UNPRIMED_CODECS`.
    """
    return not codec.startswith(PCM_CODEC_PREFIX) and codec not in UNPRIMED_CODECS


def declared_without_packets(
    header: ProbeHeader,
    *,
    estimate_warned: bool,
) -> Decimal | None:
    """Return the declared duration R5 allows, without the first-packet bound.

    This is :func:`declared_duration` for a walk that found no packet (A12d):
    there is no first packet to hold to the A9 bound, so only the other R5
    conditions apply. It is used for nothing but deciding whether an empty
    walk is truncated.

    Args:
        header: Accepted header facts.
        estimate_warned: Whether either call's stderr carried an estimate line.

    Returns:
        The declared duration, always positive, or ``None``.
    """
    if estimate_warned or header.container in NO_DECLARED_DURATION_FORMATS:
        return None
    if (
        header.container == MATROSKA_FORMAT_NAME
        and header.codec in PRIMED_MATROSKA_CODECS
    ):
        return None
    if (
        header.container in AUDIO_ONLY_DECLARED_FORMATS
        and header.non_picture_stream_count > 1
    ):
        return None
    if header.stream_duration is not None:
        return header.stream_duration
    if header.non_picture_stream_count == 1 and _starts_at_zero(
        header,
        header.format_start,
    ):
        return header.format_duration
    return None


def _starts_at_zero(header: ProbeHeader, start: Decimal | None) -> bool:
    """Report whether a start time is at most two time-base units after zero (A9).

    A negative start, from priming or an edit list, counts as starting at
    zero. Without a usable time base the bound cannot be set, so no start
    counts.

    Args:
        header: Accepted header facts.
        start: The first packet time or ``format.start_time``.

    Returns:
        ``True`` when the start time and the primary stream's time base are
        known and the start is at most two time-base units.
    """
    if start is None or header.time_base is None:
        return False
    return start <= START_TIME_BASE_UNITS * header.time_base


def decide_duration(
    header: ProbeHeader,
    walk: PacketWalk,
    run: BoundedRun,
    *,
    benign: frozenset[str],
) -> DurationOutcome:
    """Apply R6 to a finished packet walk.

    A walk with no packet is truncated only against a positive declared
    duration; otherwise its duration is unknown with ``inconsistent``
    (A12d). A measured span or median packet duration that is not positive
    is ``inconsistent`` (A12e), so no unknown duration is reported as zero
    or negative.

    Args:
        header: Accepted header facts.
        walk: The walk's packet summary.
        run: The walk call; it must not have breached a bound.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The duration outcome; the first matching R6 step wins.
    """
    lines = stderr_lines(run.stderr)
    damage = _walk_damage(
        walk,
        run,
        lines=lines,
        benign=benign,
        header=header,
    )
    if damage is not None:
        return damage
    warned = header.estimate_warned or any(
        line in ESTIMATE_STDERR_LINES for line in lines
    )
    if walk.packets == 0:
        return _empty_walk(header, estimate_warned=warned)
    return _measured_walk(header, walk, estimate_warned=warned)


def _empty_walk(header: ProbeHeader, *, estimate_warned: bool) -> DurationOutcome:
    """Decide a walk that found no packet (R6 step 2 with A12d).

    Args:
        header: Accepted header facts.
        estimate_warned: Whether either call's stderr carried an estimate line.

    Returns:
        ``truncated`` against a positive declared duration, otherwise the
        duration unknown with ``inconsistent``.
    """
    if declared_without_packets(header, estimate_warned=estimate_warned) is None:
        return DurationOutcome(None, None, (AudioFlag.INCONSISTENT,))
    return DurationOutcome(None, True)


def _measured_walk(
    header: ProbeHeader,
    walk: PacketWalk,
    *,
    estimate_warned: bool,
) -> DurationOutcome:
    """Decide a walk with packets (R6 steps 5 to 8 with A5, A12e and A15a).

    In a fragmented MP4 the declared duration is the sum of the fragments
    present, so a cut on a fragment boundary shortens it with the span. There
    the walk may find the file ``truncated`` but never complete: a result
    that would be ``truncated=false`` is ``estimated`` instead (A15a).

    Args:
        header: Accepted header facts.
        walk: The walk's packet summary.
        estimate_warned: Whether either call's stderr carried an estimate line.

    Returns:
        The duration outcome.
    """
    measured = walk.measured_duration()
    median = walk.median_duration()
    if measured is None or median is None or measured <= 0 or median <= 0:
        return DurationOutcome(None, None, (AudioFlag.INCONSISTENT,))
    declared = declared_duration(header, walk, estimate_warned=estimate_warned)
    if declared is None:
        return DurationOutcome(measured, None, (AudioFlag.ESTIMATED,))
    if declared - measured > shortfall_tolerance(header):
        return DurationOutcome(None, True)
    if is_fragmented(header, walk):
        return DurationOutcome(measured, None, (AudioFlag.ESTIMATED,))
    if measured - declared > _THREE * median:
        return DurationOutcome(None, None, (AudioFlag.INCONSISTENT,))
    return DurationOutcome(declared, False)


def shortfall_tolerance(header: ProbeHeader) -> Decimal:
    """Return how far below the declared duration a walk may end (A5).

    The tolerance is the larger of 5 ms and two time-base units of the
    primary stream; without a usable time base it is 5 ms.

    Args:
        header: Accepted header facts.

    Returns:
        The tolerance in seconds.
    """
    if header.time_base is None:
        return SHORTFALL_FLOOR_SECONDS
    return max(SHORTFALL_FLOOR_SECONDS, SHORTFALL_TIME_BASE_UNITS * header.time_base)


def _walk_damage(
    walk: PacketWalk,
    run: BoundedRun,
    *,
    lines: list[str],
    benign: frozenset[str],
    header: ProbeHeader,
) -> DurationOutcome | None:
    """Apply R6 steps 1 to 4 and the gap-packet step after step 3.

    Step 1 is split by A1 and A12c: a signal or exit 126 or 127 is
    ``backend_unavailable``, any other non-zero exit ``corrupt``, and a
    stderr line off the benign list ``unrecognized_output``. Step 2, a walk
    with no packet, is decided by :func:`decide_duration` (A12d). A walk
    with more distinct packet durations than it keeps counts as
    inconsistent (step 3). In FLV a
    packet without a time makes every audio fact unknown (A11). In MP4 a
    packet longer than two median packets is stretched across a gap, so the
    measured span is not evidence of the audio's length (A10b).

    Args:
        walk: The walk's packet summary.
        run: The walk call.
        lines: The walk's stderr lines after :func:`stderr_lines`.
        benign: Benign stderr lines for the installed FFmpeg major.
        header: Accepted header facts, for the conditional pins and the
            container.

    Returns:
        The outcome of the first matching step, or ``None`` when none matches.
    """
    if program_failed(run.returncode):
        return DurationOutcome(None, None, (AudioFlag.BACKEND_UNAVAILABLE,))
    if run.returncode != 0:
        return DurationOutcome(None, None, (AudioFlag.CORRUPT,))
    if has_unrecognized_line(lines, benign=benign, context=header.stderr_context):
        return DurationOutcome(None, None, (AudioFlag.UNRECOGNIZED_OUTPUT,))
    if (
        walk.missing_time
        or walk.backwards
        or walk.too_varied
        or (header.container in GAP_PACKET_FORMATS and spans_gap(walk))
    ):
        return DurationOutcome(
            None,
            None,
            (AudioFlag.INCONSISTENT,),
            facts_unknown=walk.missing_time
            and header.container in UNTIMED_PACKET_FORMATS,
        )
    if walk.corrupt_packet:
        return DurationOutcome(None, None, (AudioFlag.CORRUPT,))
    return None


def spans_gap(walk: PacketWalk) -> bool:
    """Report whether a packet is long enough to span a gap (A10b).

    A muxer can stretch a packet across a start delay (delayed fragmented
    MP4 does this with its first AAC packet), so the measured span then
    includes the delay. The caller limits this to :data:`GAP_PACKET_FORMATS`.

    Args:
        walk: The walk's packet summary.

    Returns:
        ``True`` when the longest packet exceeds
        :data:`GAP_PACKET_MEDIAN_RATIO` median packet durations.
    """
    longest = walk.longest_duration
    median = walk.median_duration()
    if longest is None or median is None:
        return False
    return longest > GAP_PACKET_MEDIAN_RATIO * median


def parse_positive_int(value: object) -> int | None:
    """Parse a positive integer given as a JSON integer or decimal string.

    Args:
        value: Raw ffprobe value.

    Returns:
        The integer, or ``None`` for zero, ``N/A``, or anything else.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _INT_PATTERN.fullmatch(value):
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def parse_decimal(value: object) -> Decimal | None:
    """Parse a plain decimal number given as a string or JSON integer.

    Args:
        value: Raw ffprobe value such as ``"2.000000"``.

    Returns:
        The exact value, or ``None`` for ``N/A`` or anything else.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str) and _DECIMAL_PATTERN.fullmatch(value):
        return Decimal(value)
    return None


def parse_time_base(value: object) -> Decimal | None:
    """Parse a ``time_base`` such as ``1/48000`` to seconds.

    Args:
        value: Raw ffprobe value.

    Returns:
        The unit in seconds, or ``None`` unless both parts are positive
        integers.
    """
    if not isinstance(value, str):
        return None
    match = _TIME_BASE.fullmatch(value)
    if match is None:
        return None
    numerator = int(match.group("num"))
    denominator = int(match.group("den"))
    if numerator == 0 or denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def parse_positive_decimal(value: object) -> Decimal | None:
    """Parse a decimal number that must be greater than zero.

    Args:
        value: Raw ffprobe value.

    Returns:
        The value, or ``None`` when it is not a positive decimal.
    """
    parsed = parse_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _is_attached_pic(stream: dict[str, object]) -> bool:
    """Report whether a stream is cover art.

    Args:
        stream: An ffprobe stream object.

    Returns:
        ``True`` when ``disposition.attached_pic`` is 1.
    """
    return _disposition(stream, "attached_pic")


def _disposition(stream: dict[str, object], key: str) -> bool:
    """Read one disposition bit of a stream.

    Args:
        stream: An ffprobe stream object.
        key: Disposition name.

    Returns:
        ``True`` when the disposition value is exactly 1.
    """
    disposition = stream.get("disposition")
    if not isinstance(disposition, dict):
        return False
    value = disposition.get(key)
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def parse_probe_score(value: object) -> int | None:
    """Return ``probe_score`` when it is a JSON integer from 0 to 100 (A12f).

    Args:
        value: Raw ffprobe value.

    Returns:
        The score, or ``None`` for anything else.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= PROBE_SCORE_MAX else None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing repeated keys.

    Args:
        pairs: Key-value pairs in document order.

    Returns:
        The object.

    Raises:
        _DuplicateKeyError: If a key appears twice.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    """Refuse ``NaN`` and ``Infinity`` in JSON.

    Args:
        name: The constant's name.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"non-finite JSON constant {name}")


__all__ = [
    "ACCEPTED_FORMAT_NAMES",
    "AUDIO_ONLY_DECLARED_FORMATS",
    "BENIGN_STDERR_BY_MAJOR",
    "BIT_DEPTH_CODECS",
    "CONDITIONAL_STDERR_LINES",
    "COVER_TIMESCALE_WARNING",
    "DATA_TRACK_WARNING",
    "DurationOutcome",
    "ESTIMATE_STDERR_LINES",
    "GAP_PACKET_FORMATS",
    "GAP_PACKET_MEDIAN_RATIO",
    "HEADER_SHOW_ENTRIES",
    "HE_AAC_PROFILES",
    "MATROSKA_FORMAT_NAME",
    "MAX_DISTINCT_PACKET_DURATIONS",
    "MP4_FORMAT_NAME",
    "NO_DECLARED_DURATION_FORMATS",
    "PRIMED_MATROSKA_CODECS",
    "PCM_CODEC_PREFIX",
    "PRIMING_SPAN_FORMATS",
    "PROBE_SCORE_MAX",
    "PROGRAM_FAILURE_EXIT_CODES",
    "PacketWalk",
    "ProbeHeader",
    "SHORTFALL_FLOOR_SECONDS",
    "SHORTFALL_TIME_BASE_UNITS",
    "START_TIME_BASE_UNITS",
    "StderrContext",
    "UNPRIMED_CODECS",
    "UNTIMED_PACKET_FORMATS",
    "bit_depth_for",
    "decide_duration",
    "declared_duration",
    "declared_without_packets",
    "first_packet_stretched",
    "has_unrecognized_line",
    "header_from_json",
    "header_rejection",
    "interpret_header",
    "is_fragmented",
    "is_he_aac",
    "load_probe_json",
    "may_be_primed",
    "parse_decimal",
    "parse_positive_decimal",
    "parse_positive_int",
    "parse_time_base",
    "primary_audio_stream",
    "parse_probe_score",
    "program_failed",
    "shortfall_tolerance",
    "spans_gap",
    "stderr_context",
    "stderr_lines",
]
