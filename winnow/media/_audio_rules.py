"""Pure rules of the guarded ffprobe audio reader (issue #400).

Everything here works on ffprobe output that has already been captured:
stderr filtering against the benign list (R2), strict JSON parsing (R1),
stream selection and facts (R2 to R4), the declared duration (R5), and the
packet-walk decision (R6). Nothing here starts a process or touches a file;
:mod:`winnow.media.audio` runs ffprobe and feeds these functions.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
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


MATROSKA_FORMAT_NAME: Final[str] = "matroska,webm"


NO_DECLARED_DURATION_FORMATS: Final[frozenset[str]] = frozenset(
    {"ogg", "mpegts", "mpeg", "flv", "aac", "ac3", "eac3"},
)
"""Containers whose duration is never taken as declared (R5)."""


ESTIMATE_STDERR_LINES: Final[frozenset[str]] = frozenset(
    {
        "Estimating duration from bitrate, this may be inaccurate",
        "invalid concatenated file detected - using bitrate for duration",
    },
)
"""Benign lines that mean ffprobe estimated the duration (R2, R5)."""


BENIGN_STDERR_BY_MAJOR: Final[dict[int, frozenset[str]]] = {
    9: ESTIMATE_STDERR_LINES
    | frozenset({"ignoring wrong sample_count", "Ignoring maximum wav data size"}),
}
"""Stderr lines that do not invalidate a run, per FFmpeg major (R2, R11).

A major without an entry has no benign lines: any stderr line makes the facts
unknown.
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


_MATROSKA_DURATION: Final[re.Pattern[str]] = re.compile(
    r"(?P<h>[0-9]{1,6}):(?P<m>[0-5][0-9]):(?P<s>[0-5][0-9](?:\.[0-9]{1,12})?)",
)


_THREE: Final[Decimal] = Decimal(3)


class _DuplicateKeyError(ValueError):
    """Raised when ffprobe JSON repeats a key inside one object."""


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
        stream_duration: The primary stream's ``duration``.
        tag_duration: The primary stream's Matroska ``DURATION`` tag.
        format_duration: ``format.duration``.
        format_start: ``format.start_time``.
        estimate_warned: ``True`` when stderr says the duration was estimated.
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
    stream_duration: Decimal | None
    tag_duration: Decimal | None
    format_duration: Decimal | None
    format_start: Decimal | None
    estimate_warned: bool


@dataclass(slots=True)
class PacketWalk:
    """Running summary of a packet walk, fed one CSV line at a time (R6).

    Only counts and a few times are kept, so a long walk needs little memory.
    """

    packets: int = 0
    first_time: Decimal | None = None
    last_time: Decimal | None = None
    last_duration: Decimal | None = None
    missing_time: bool = False
    backwards: bool = False
    corrupt_packet: bool = False
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

    Args:
        run: The header call; it must not have breached a bound.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The accepted header, or ``None`` with the flags that explain why not.
    """
    lines = stderr_lines(run.stderr)
    if run.returncode != 0 or any(line not in benign for line in lines):
        return None, (AudioFlag.CORRUPT,)
    data = load_probe_json(run.stdout)
    if data is None:
        return None, (AudioFlag.CORRUPT,)
    estimate_warned = any(line in ESTIMATE_STDERR_LINES for line in lines)
    return header_from_json(data, estimate_warned=estimate_warned), ()


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
        stream_duration=parse_positive_decimal(primary.get("duration")),
        tag_duration=_matroska_tag_duration(primary.get("tags")),
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

    Args:
        header: Accepted header facts.
        walk: Completed packet walk of the primary stream.
        estimate_warned: Whether either call's stderr carried an estimate line.

    Returns:
        The declared duration, or ``None`` when nothing declares one.
    """
    if estimate_warned or header.container in NO_DECLARED_DURATION_FORMATS:
        return None
    if header.stream_duration is not None:
        return header.stream_duration
    if header.container == MATROSKA_FORMAT_NAME:
        from_tag = _matroska_declared(header, walk)
        if from_tag is not None:
            return from_tag
    if header.non_picture_stream_count == 1:
        return header.format_duration
    return None


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
    damage = _walk_damage(walk, run, lines=lines, benign=benign)
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
    if declared - measured > median:
        return DurationOutcome(None, True)
    if measured - declared > _THREE * median:
        return DurationOutcome(None, None, (AudioFlag.INCONSISTENT,))
    return DurationOutcome(declared, False)


def _walk_damage(
    walk: PacketWalk,
    run: BoundedRun,
    *,
    lines: list[str],
    benign: frozenset[str],
) -> DurationOutcome | None:
    """Apply R6 steps 1 to 4.

    Args:
        walk: The walk's packet summary.
        run: The walk call.
        lines: The walk's stderr lines after :func:`stderr_lines`.
        benign: Benign stderr lines for the installed FFmpeg major.

    Returns:
        The outcome of the first matching step, or ``None`` when none matches.
    """
    if run.returncode != 0 or any(line not in benign for line in lines):
        return DurationOutcome(None, None, (AudioFlag.CORRUPT,))
    if walk.packets == 0:
        return DurationOutcome(None, True)
    if walk.missing_time or walk.backwards:
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


def parse_positive_decimal(value: object) -> Decimal | None:
    """Parse a decimal number that must be greater than zero.

    Args:
        value: Raw ffprobe value.

    Returns:
        The value, or ``None`` when it is not a positive decimal.
    """
    parsed = parse_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _matroska_declared(header: ProbeHeader, walk: PacketWalk) -> Decimal | None:
    """Return the Matroska ``DURATION`` tag minus the stream's lead (R5).

    The lead is the first packet time minus ``format.start_time``; the tag
    is used only when the lead is more than three median packet durations.

    Args:
        header: Accepted header facts.
        walk: Completed packet walk.

    Returns:
        The declared duration, or ``None`` when the rule does not apply.
    """
    median = walk.median_duration()
    if (
        header.tag_duration is None
        or header.format_start is None
        or walk.first_time is None
        or median is None
    ):
        return None
    lead = walk.first_time - header.format_start
    if lead <= _THREE * median:
        return None
    declared = header.tag_duration - lead
    return declared if declared > 0 else None


def _matroska_tag_duration(tags: object) -> Decimal | None:
    """Parse a Matroska ``DURATION`` stream tag such as ``00:00:02.008000000``.

    Args:
        tags: The stream's ``tags`` object.

    Returns:
        The duration in seconds, or ``None`` when absent or malformed.
    """
    if not isinstance(tags, dict):
        return None
    value = tags.get("DURATION")
    if not isinstance(value, str):
        return None
    match = _MATROSKA_DURATION.fullmatch(value)
    if match is None:
        return None
    hours = Decimal(match.group("h"))
    minutes = Decimal(match.group("m"))
    seconds = Decimal(match.group("s"))
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


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
    "BENIGN_STDERR_BY_MAJOR",
    "BIT_DEPTH_CODECS",
    "DurationOutcome",
    "ESTIMATE_STDERR_LINES",
    "MATROSKA_FORMAT_NAME",
    "NO_DECLARED_DURATION_FORMATS",
    "PacketWalk",
    "ProbeHeader",
    "bit_depth_for",
    "decide_duration",
    "declared_duration",
    "header_from_json",
    "interpret_header",
    "load_probe_json",
    "parse_decimal",
    "parse_positive_decimal",
    "parse_positive_int",
    "primary_audio_stream",
    "stderr_lines",
]
