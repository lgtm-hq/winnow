"""Pure rules of the guarded ffprobe audio reader (issue #400).

Everything here works on ffprobe output that has already been captured:
stderr filtering against the benign list (R2 with amendments A1 to A3),
strict JSON parsing (R1), stream selection and facts (R2 to R4), the declared
duration (R5 with A4 and A5), and the packet-walk decision (R6 with A5).
Nothing here starts a process or touches a file; :mod:`winnow.media.audio`
runs ffprobe and feeds these functions.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
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
    {"ogg", "mpegts", "mpeg", "flv", "aac", "ac3", "eac3"},
)
"""Containers whose duration is never taken as declared (R5)."""


AUDIO_ONLY_DECLARED_FORMATS: Final[frozenset[str]] = frozenset({"avi", "asf"})
"""Containers whose audio duration is declared only without other streams (A8).

In AVI the audio stream's duration includes a start delay the packet times
leave out; in ASF it is the whole file's play length, set by the video's end.
Any non-picture stream besides the primary audio stream makes the duration
undeclared.
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


_THREE: Final[Decimal] = Decimal(3)


SHORTFALL_FLOOR_SECONDS: Final[Decimal] = Decimal("0.005")
"""Smallest shortfall below the declared duration that counts (R6 step 6, A5)."""


SHORTFALL_TIME_BASE_UNITS: Final[Decimal] = Decimal(2)
"""Time-base units of the primary stream a shortfall must exceed (A5)."""


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
    missing_time: bool = False
    backwards: bool = False
    corrupt_packet: bool = False
    too_varied: bool = False
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
        if time is None or duration is None:
            self.missing_time = True
            return
        if self.last_time is not None and time < self.last_time:
            self.backwards = True
        if self.first_time is None:
            self.first_time = time
        self.last_time = time
        self.last_duration = duration
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
    """

    duration: Decimal | None
    truncated: bool | None
    flags: tuple[AudioFlag, ...] = ()


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

    A non-zero exit or JSON that fails strict parsing is ``corrupt``; a
    stderr line off the benign list is ``unrecognized_output`` (A1).

    Args:
        run: The header call; it must not have breached a bound.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The accepted header, or ``None`` with the flags that explain why not.
    """
    if run.returncode != 0:
        return None, (AudioFlag.CORRUPT,)
    data = load_probe_json(run.stdout)
    if data is None:
        return None, (AudioFlag.CORRUPT,)
    lines = stderr_lines(run.stderr)
    context = stderr_context(data)
    if has_unrecognized_line(lines, benign=benign, context=context):
        return None, (AudioFlag.UNRECOGNIZED_OUTPUT,)
    estimate_warned = any(line in ESTIMATE_STDERR_LINES for line in lines)
    header = header_from_json(data, estimate_warned=estimate_warned)
    if header is None:
        return None, ()
    return replace(header, stderr_context=context), ()


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
        probe_score=_int_or_none(container_section.get("probe_score")),
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
    )


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
    if not codec.startswith("pcm_") and codec not in BIT_DEPTH_CODECS:
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

    The stream's own ``duration`` is declared. Otherwise ``format.duration``
    is declared only for a file with one non-picture stream that starts at
    zero (A4). A Matroska ``DURATION`` tag is never used (A5): for audio that
    starts late, the tag minus the lead overshoots the real end by the
    codec's priming. In AVI and ASF nothing is declared when the file has
    any non-picture stream besides the primary audio stream (A8).

    Args:
        header: Accepted header facts.
        walk: Completed packet walk of the primary stream.
        estimate_warned: Whether either call's stderr carried an estimate line.

    Returns:
        The declared duration, or ``None`` when nothing declares one.
    """
    if estimate_warned or header.container in NO_DECLARED_DURATION_FORMATS:
        return None
    if (
        header.container in AUDIO_ONLY_DECLARED_FORMATS
        and header.non_picture_stream_count > 1
    ):
        return None
    if header.stream_duration is not None:
        return header.stream_duration
    if header.non_picture_stream_count == 1 and _starts_at_zero(header, walk):
        return header.format_duration
    return None


def _starts_at_zero(header: ProbeHeader, walk: PacketWalk) -> bool:
    """Report whether ``format.start_time`` is zero within one packet (A4).

    ``format.duration`` is an end time, so it is a duration only when the
    file starts at zero.

    Args:
        header: Accepted header facts.
        walk: Completed packet walk of the primary stream.

    Returns:
        ``True`` when the start time is known and at most one median packet
        duration away from zero.
    """
    median = walk.median_duration()
    if header.format_start is None or median is None:
        return False
    return abs(header.format_start) <= median


def decide_duration(
    header: ProbeHeader,
    walk: PacketWalk,
    run: BoundedRun,
    *,
    benign: frozenset[str],
) -> DurationOutcome:
    """Apply R6 to a finished packet walk.

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
        context=header.stderr_context,
    )
    if damage is not None:
        return damage
    measured = walk.measured_duration()
    median = walk.median_duration()
    if measured is None or median is None:
        return DurationOutcome(None, None, (AudioFlag.INCONSISTENT,))
    warned = header.estimate_warned or any(
        line in ESTIMATE_STDERR_LINES for line in lines
    )
    declared = declared_duration(header, walk, estimate_warned=warned)
    if declared is None:
        return DurationOutcome(measured, None, (AudioFlag.ESTIMATED,))
    if declared - measured > shortfall_tolerance(header):
        return DurationOutcome(None, True)
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
    context: StderrContext,
) -> DurationOutcome | None:
    """Apply R6 steps 1 to 4.

    Step 1 is split by A1: a non-zero exit is ``corrupt``, a stderr line off
    the benign list is ``unrecognized_output``. A walk with more distinct
    packet durations than it keeps counts as inconsistent (step 3).

    Args:
        walk: The walk's packet summary.
        run: The walk call.
        lines: The walk's stderr lines after :func:`stderr_lines`.
        benign: Benign stderr lines for the installed FFmpeg major.
        context: Conditions for the conditional pins, from the header JSON.

    Returns:
        The outcome of the first matching step, or ``None`` when none matches.
    """
    if run.returncode != 0:
        return DurationOutcome(None, None, (AudioFlag.CORRUPT,))
    if has_unrecognized_line(lines, benign=benign, context=context):
        return DurationOutcome(None, None, (AudioFlag.UNRECOGNIZED_OUTPUT,))
    if walk.packets == 0:
        return DurationOutcome(None, True)
    if walk.missing_time or walk.backwards or walk.too_varied:
        return DurationOutcome(None, None, (AudioFlag.INCONSISTENT,))
    if walk.corrupt_packet:
        return DurationOutcome(None, None, (AudioFlag.CORRUPT,))
    return None


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


def _int_or_none(value: object) -> int | None:
    """Return a JSON integer as is, anything else as ``None``.

    Args:
        value: Raw ffprobe value.

    Returns:
        The integer or ``None``.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


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
    "MAX_DISTINCT_PACKET_DURATIONS",
    "MP4_FORMAT_NAME",
    "NO_DECLARED_DURATION_FORMATS",
    "PacketWalk",
    "ProbeHeader",
    "SHORTFALL_FLOOR_SECONDS",
    "SHORTFALL_TIME_BASE_UNITS",
    "StderrContext",
    "bit_depth_for",
    "decide_duration",
    "declared_duration",
    "has_unrecognized_line",
    "header_from_json",
    "interpret_header",
    "load_probe_json",
    "parse_decimal",
    "parse_positive_decimal",
    "parse_positive_int",
    "parse_time_base",
    "primary_audio_stream",
    "shortfall_tolerance",
    "stderr_context",
    "stderr_lines",
]
