"""Unit tests for the pure audio rules (issue #400), no ffprobe needed.

Hand-built ffprobe output is fed to the functions in
:mod:`winnow.media._audio_rules`: stderr filtering (R2, A1 to A3), strict JSON
(R1), stream selection and facts (R2 to R4), the declared duration (R5, A4,
A5, A8), and the packet-walk decision (R6, A5, A7).
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from assertpy import assert_that

from tests.media.audio_support import audio_stream, probe_json, run_result
from winnow.media import audio as audio_module
from winnow.media._audio_rules import (
    AUDIO_ONLY_DECLARED_FORMATS,
    COVER_TIMESCALE_WARNING,
    DATA_TRACK_WARNING,
    MAX_DISTINCT_PACKET_DURATIONS,
    SHORTFALL_FLOOR_SECONDS,
    StderrContext,
    has_unrecognized_line,
    parse_time_base,
    shortfall_tolerance,
    stderr_context,
)
from winnow.media.audio import (
    BENIGN_STDERR_BY_MAJOR,
    ESTIMATE_STDERR_LINES,
    PacketWalk,
    ProbeHeader,
    benign_stderr_lines,
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
    walk_timeout,
)
from winnow.models.media import AudioFlag

FAKE_BINARY = "/opt/ffmpeg/bin/ffprobe"
BANNER_9 = "ffprobe version 9.0.2 Copyright (c) 2007-2026 the FFmpeg developers"
BENIGN_9 = BENIGN_STDERR_BY_MAJOR[9]
ESTIMATE_LINE = "Estimating duration from bitrate, this may be inaccurate"


def _header(**overrides: Any) -> ProbeHeader:
    """Build accepted header facts.

    Args:
        overrides: Fields to replace.

    Returns:
        The header.
    """
    header = ProbeHeader(
        container="flac",
        probe_score=100,
        stream_index=0,
        codec="flac",
        sample_rate=44100,
        channels=2,
        bit_depth=16,
        bitrate=900000,
        audio_stream_count=1,
        non_picture_stream_count=1,
        time_base=Decimal(1) / Decimal(44100),
        stream_duration=Decimal("2.000000"),
        format_duration=Decimal("2.000000"),
        format_start=Decimal("0.000000"),
        estimate_warned=False,
    )
    return replace(header, **overrides)


def _accepted(data: dict[str, object]) -> ProbeHeader:
    """Return the header facts, failing the test when they are refused.

    Args:
        data: ffprobe header object.

    Returns:
        The accepted header.
    """
    header = header_from_json(data, estimate_warned=False)
    if header is None:
        pytest.fail("header was refused")
    return header


def _walk(*lines: str) -> PacketWalk:
    """Feed CSV lines into a packet walk.

    Args:
        lines: ``pts_time,duration_time,flags`` lines.

    Returns:
        The walk.
    """
    walk = PacketWalk()
    for line in lines:
        walk.feed(line.encode())
    return walk


def _packets(count: int, *, step: str = "0.020000", start: str = "0") -> list[str]:
    """Build evenly spaced healthy packet lines.

    Args:
        count: Number of packets.
        step: Packet duration.
        start: First packet time.

    Returns:
        CSV lines.
    """
    first = Decimal(start)
    size = Decimal(step)
    return [f"{first + size * index:.6f},{size:.6f},K__" for index in range(count)]


# --- stderr handling (R2) ---------------------------------------------------


def test_stderr_lines_strips_prefix_and_repeats() -> None:
    """Context prefixes, repeat notices, and blank lines are dropped."""
    raw = (
        b"[mp3 @ 0x7f00aa] Estimating duration from bitrate, this may be inaccurate\n"
        b"    Last message repeated 3 times\n"
        b"\n"
        b"[in#0 @ 0xABC] [aac @ 0x12] Packet corrupt (stream = 0, dts = 1).\n"
        b"plain line  \n"
    )

    assert_that(stderr_lines(raw)).is_equal_to(
        [ESTIMATE_LINE, "Packet corrupt (stream = 0, dts = 1).", "plain line"],
    )


def test_stderr_lines_tolerates_invalid_utf8() -> None:
    """Undecodable bytes become replacement characters, not an error."""
    assert_that(stderr_lines(b"bad \xff byte\n")).is_equal_to(["bad � byte"])


@pytest.mark.parametrize(
    ("banner", "expected"),
    [
        (BANNER_9, BENIGN_9),
        ("ffprobe version n9.1 Copyright", BENIGN_9),
        ("ffprobe version 8.0 Copyright", frozenset()),
        ("ffprobe version N-120000-gdeadbeef Copyright", frozenset()),
        (None, frozenset()),
    ],
    ids=["pinned", "pinned_n_prefix", "unpinned_major", "git_build", "unreadable"],
)
def test_benign_stderr_lines_per_major(
    monkeypatch: pytest.MonkeyPatch,
    banner: str | None,
    expected: frozenset[str],
) -> None:
    """Only a pinned major has benign lines; anything else has none."""
    monkeypatch.setattr(audio_module, "ffprobe_version", lambda binary: banner)

    assert_that(benign_stderr_lines(FAKE_BINARY)).is_equal_to(expected)


def test_benign_list_for_9_is_the_issue_list() -> None:
    """The 9.x pins are the four R2 lines plus the A2 and A3 pins.

    The WAV line is the exact text ffprobe 9.0.2 prints.
    """
    assert_that(BENIGN_9).is_equal_to(
        frozenset(
            {
                "Estimating duration from bitrate, this may be inaccurate",
                "invalid concatenated file detected - using bitrate for duration",
                "ignoring wrong sample_count",
                "Ignoring maximum wav data size, file may be invalid",
                "Unsupported codec with id <N> for input stream <M>",
                "stream 0, timescale not set",
            },
        ),
    )
    assert_that(ESTIMATE_STDERR_LINES <= BENIGN_9).is_true()


def test_wav_data_size_line_is_benign() -> None:
    """The WAV warning matches as ffprobe prints it."""
    lines = stderr_lines(
        b"[wav @ 0x1] Ignoring maximum wav data size, file may be invalid\n",
    )

    assert_that(
        has_unrecognized_line(lines, benign=BENIGN_9, context=StderrContext()),
    ).is_false()


# --- conditional pins (A2, A3) -----------------------------------------------

_DATA_LINE = "Unsupported codec with id 0 for input stream 2"


def _data_track_json(codec_type: str = "data") -> dict[str, object]:
    """Build an MOV header with video, audio, and a third track.

    Args:
        codec_type: Type of stream 2.

    Returns:
        The header object.
    """
    return probe_json(
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4"},
        audio_stream(index=1, codec_name="aac"),
        {"index": 2, "codec_type": codec_type, "codec_name": "unknown"},
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
    )


@pytest.mark.parametrize(
    ("codec_type", "line", "unrecognized"),
    [
        ("data", _DATA_LINE, False),
        ("video", _DATA_LINE, False),
        ("subtitle", _DATA_LINE, False),
        ("audio", _DATA_LINE, True),
        ("data", "Unsupported codec with id 0 for input stream 1", True),
        ("data", "Unsupported codec with id 0 for input stream 7", True),
        ("data", "Unsupported codec with id 0 for input stream 2 now", True),
        ("data", "Unsupported codec with id x for input stream 2", True),
        ("data", DATA_TRACK_WARNING, True),
    ],
    ids=[
        "data_track",
        "video_track",
        "subtitle_track",
        "audio_track",
        "names_the_audio_stream",
        "absent_stream",
        "trailing_text",
        "non_numeric_id",
        "pin_text_itself",
    ],
)
def test_data_track_warning(codec_type: str, line: str, unrecognized: bool) -> None:
    """A2: benign only for a stream the JSON has and that is not audio."""
    context = stderr_context(_data_track_json(codec_type))

    assert_that(
        has_unrecognized_line([line], benign=BENIGN_9, context=context),
    ).is_equal_to(unrecognized)


def test_data_track_warning_needs_its_pin() -> None:
    """On a major without the A2 pin the line is unrecognized."""
    context = stderr_context(_data_track_json())

    assert_that(
        has_unrecognized_line(
            [_DATA_LINE],
            benign=frozenset({ESTIMATE_LINE}),
            context=context,
        ),
    ).is_true()


def test_interpret_header_accepts_data_track_warning() -> None:
    """A2 in the header call: the facts of the audio stream are accepted."""
    run = run_result(
        stdout=json.dumps(_data_track_json()).encode(),
        stderr=f"{_DATA_LINE}\n".encode(),
    )

    header, flags = interpret_header(run, benign=BENIGN_9)

    assert_that(flags).is_empty()
    if header is None:
        pytest.fail("header was refused")
    assert_that((header.codec, header.stream_index)).is_equal_to(("aac", 1))
    assert_that(header.stderr_context.non_audio_indexes).is_equal_to(
        frozenset({0, 2}),
    )


def test_data_track_warning_in_the_walk() -> None:
    """A2 in the walk: the header's context decides the walk's lines too."""
    context = stderr_context(_data_track_json())
    header = _header(stderr_context=context)

    accepted = _decide(
        _walk(*_packets(100)),
        header=header,
        stderr=f"{_DATA_LINE}\n".encode(),
    )
    refused = _decide(
        _walk(*_packets(100)),
        header=header,
        stderr=b"Unsupported codec with id 0 for input stream 1\n",
    )

    assert_that(accepted).is_equal_to((Decimal("2.000000"), False, ()))
    assert_that(refused).is_equal_to(
        (None, None, (AudioFlag.UNRECOGNIZED_OUTPUT,)),
    )


def _cover_json(
    *,
    cover: dict[str, object] | None = None,
    audio: dict[str, object] | None = None,
    format_name: str = "mov,mp4,m4a,3gp,3g2,mj2",
) -> dict[str, object]:
    """Build an M4A header with AAC at index 0 and covr art at index 1.

    Args:
        cover: Overrides for the cover stream.
        audio: Overrides for the audio stream.
        format_name: Container name.

    Returns:
        The header object.
    """
    cover_stream: dict[str, object] = {
        "index": 1,
        "codec_type": "video",
        "codec_name": "png",
        "id": "0x0",
        "time_base": "1/90000",
        "disposition": {"default": 0, "attached_pic": 1},
    }
    cover_stream.update(cover or {})
    audio_fields: dict[str, object] = {
        "codec_name": "aac",
        "sample_rate": "48000",
        "time_base": "1/48000",
        "id": "0x1",
        "disposition": {"default": 1, "attached_pic": 0},
    }
    audio_fields.update(audio or {})
    return probe_json(
        audio_stream(**audio_fields),
        cover_stream,
        format_name=format_name,
    )


def test_cover_timescale_warning_is_benign_for_covr_art() -> None:
    """A3: the covr line is benign when its condition holds."""
    context = stderr_context(_cover_json())

    assert_that(context.cover_timescale_benign).is_true()
    assert_that(
        has_unrecognized_line(
            [COVER_TIMESCALE_WARNING],
            benign=BENIGN_9,
            context=context,
        ),
    ).is_false()


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"cover": {"id": "0x2"}},
        {"cover": {"disposition": {"default": 0, "attached_pic": 0}}},
        {"audio": {"time_base": "1/1000"}},
        {"audio": {"time_base": "1/1"}},
        {"audio": {"time_base": None}},
        {"audio": {"index": 2}},
        {"format_name": "matroska,webm"},
    ],
    ids=[
        "picture_is_a_track",
        "no_attached_picture",
        "movie_time_scale_fallback",
        "unit_time_scale_fallback",
        "no_time_base",
        "stream_0_is_not_audio",
        "not_mp4",
    ],
)
def test_cover_timescale_warning_outside_its_condition(
    kwargs: dict[str, Any],
) -> None:
    """A3: without covr art, or with a fallback time base, it is unrecognized."""
    context = stderr_context(_cover_json(**kwargs))

    assert_that(context.cover_timescale_benign).is_false()
    assert_that(
        has_unrecognized_line(
            [COVER_TIMESCALE_WARNING],
            benign=BENIGN_9,
            context=context,
        ),
    ).is_true()


def test_cover_timescale_warning_needs_primary_stream_0() -> None:
    """A3: stream 0 must be the primary audio stream."""
    data = _cover_json(audio={"disposition": {"default": 0, "attached_pic": 0}})
    streams = data["streams"]
    if not isinstance(streams, list):
        pytest.fail("streams is not a list")
    streams.append(
        audio_stream(
            index=2,
            sample_rate="48000",
            disposition={"default": 1, "attached_pic": 0},
        ),
    )

    assert_that(stderr_context(data).cover_timescale_benign).is_false()


def test_cover_timescale_warning_needs_its_pin() -> None:
    """On a major without the A3 pin the line is unrecognized."""
    context = stderr_context(_cover_json())

    assert_that(
        has_unrecognized_line(
            [COVER_TIMESCALE_WARNING],
            benign=frozenset({ESTIMATE_LINE}),
            context=context,
        ),
    ).is_true()


@pytest.mark.parametrize(
    "data",
    [{}, {"format": {"format_name": "flac"}, "streams": "x"}, {"format": []}],
)
def test_stderr_context_of_malformed_json_is_empty(data: dict[str, object]) -> None:
    """Without the expected shape no conditional pin can apply."""
    assert_that(stderr_context(data)).is_equal_to(StderrContext())


def test_stderr_context_skips_streams_without_an_integer_index() -> None:
    """Only streams with an integer index can be named by the A2 line."""
    data = probe_json(
        audio_stream(),
        {"index": True, "codec_type": "data"},
        {"index": "2", "codec_type": "data"},
        {"index": 3},
    )

    assert_that(stderr_context(data).non_audio_indexes).is_empty()


# --- strict JSON (R1) -------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b'{"format": {"a": 1, "a": 2}}',
        b'{"streams": [], "streams": []}',
        b'{"format": {"tags": {"title": "\xff"}}}',
        b'{"format": {"duration": NaN}}',
        b'{"format": {"duration": Infinity}}',
        b"[1, 2]",
        b"not json",
        b"",
    ],
    ids=[
        "nested_duplicate",
        "top_duplicate",
        "invalid_utf8",
        "nan",
        "infinity",
        "not_object",
        "garbage",
        "empty",
    ],
)
def test_load_probe_json_rejects(payload: bytes) -> None:
    """Repeated keys, bad UTF-8, non-finite numbers, and non-objects fail."""
    assert_that(load_probe_json(payload)).is_none()


def test_load_probe_json_accepts_object() -> None:
    """A well-formed object parses."""
    assert_that(load_probe_json(b'{"format": {"a": "b"}}')).is_equal_to(
        {"format": {"a": "b"}},
    )


# --- value parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (44100, 44100),
        ("44100", 44100),
        (0, None),
        ("0", None),
        ("N/A", None),
        ("-5", None),
        ("12.5", None),
        (True, None),
        (None, None),
        ("9" * 40, None),
        (3.0, None),
    ],
)
def test_parse_positive_int(value: object, expected: int | None) -> None:
    """Only positive JSON integers or digit strings parse."""
    assert_that(parse_positive_int(value)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2.000000", Decimal("2.000000")),
        ("-0.006500", Decimal("-0.006500")),
        (3, Decimal(3)),
        ("N/A", None),
        ("", None),
        ("1e5", None),
        ("nan", None),
        (False, None),
        (2.5, None),
    ],
)
def test_parse_decimal(value: object, expected: Decimal | None) -> None:
    """Plain decimal strings and integers parse; nothing else does."""
    assert_that(parse_decimal(value)).is_equal_to(expected)


@pytest.mark.parametrize("value", ["0.000000", "-1.5", "N/A"])
def test_parse_positive_decimal_rejects(value: str) -> None:
    """Zero, negative, and missing durations are unknown."""
    assert_that(parse_positive_decimal(value)).is_none()


def test_walk_timeout_grows_with_size_and_caps() -> None:
    """10 s plus 1 s per 20 MB, capped at 300 s (R8)."""
    assert_that(walk_timeout(0)).is_equal_to(10.0)
    assert_that(walk_timeout(20_000_000)).is_equal_to(11.0)
    assert_that(walk_timeout(10**12)).is_equal_to(300.0)


# --- stream selection and facts (R2 to R4) ----------------------------------


def test_primary_prefers_lowest_index_default() -> None:
    """The lowest-index default stream wins over a lower non-default one."""
    streams = [
        audio_stream(index=3, disposition={"default": 1}),
        audio_stream(index=0, disposition={"default": 0}),
        audio_stream(index=2, disposition={"default": 1}),
    ]

    assert_that(primary_audio_stream(streams)).is_same_as(streams[2])


def test_primary_falls_back_to_lowest_index() -> None:
    """Without a default stream the lowest index is primary."""
    streams = [audio_stream(index=4), audio_stream(index=1, disposition="bogus")]

    assert_that(primary_audio_stream(streams)).is_same_as(streams[1])


@pytest.mark.parametrize("index", [-1, True, "0", None])
def test_primary_refuses_bad_index(index: object) -> None:
    """A stream without a usable integer index makes the choice unknown."""
    assert_that(primary_audio_stream([audio_stream(index=index)])).is_none()


def test_primary_without_streams() -> None:
    """No audio stream means no primary stream."""
    assert_that(primary_audio_stream([])).is_none()


@pytest.mark.parametrize(
    ("codec", "raw", "plain", "expected"),
    [
        ("pcm_s24le", "24", 24, 24),
        ("pcm_u8", None, 8, 8),
        ("flac", "0", 16, 16),
        ("alac", "N/A", 0, None),
        ("wavpack", "16", 0, 16),
        ("truehd", "24", 0, 24),
        ("aac", "16", 16, None),
        ("mp3", None, 16, None),
        ("adpcm_ms", None, 4, None),
    ],
)
def test_bit_depth_rules(
    codec: str,
    raw: str | None,
    plain: int,
    expected: int | None,
) -> None:
    """Bit depth only for PCM and listed lossless codecs; zero is unknown."""
    stream = audio_stream(
        codec_name=codec, bits_per_raw_sample=raw, bits_per_sample=plain
    )

    assert_that(bit_depth_for(stream, codec=codec)).is_equal_to(expected)


def test_header_from_json_reads_primary_facts() -> None:
    """Facts come from the primary stream and container from format_name."""
    data = probe_json(
        audio_stream(index=0, sample_rate="8000", channels=1),
        audio_stream(
            index=1,
            codec_name="alac",
            sample_rate="48000",
            channels=6,
            bits_per_raw_sample="24",
            bit_rate="1500000",
            disposition={"default": 1},
        ),
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        probe_score=100,
    )

    header = _accepted(data)

    assert_that(header.container).is_equal_to("mov,mp4,m4a,3gp,3g2,mj2")
    assert_that(header.stream_index).is_equal_to(1)
    assert_that(header.codec).is_equal_to("alac")
    assert_that(header.sample_rate).is_equal_to(48000)
    assert_that(header.channels).is_equal_to(6)
    assert_that(header.bit_depth).is_equal_to(24)
    assert_that(header.bitrate).is_equal_to(1500000)
    assert_that(header.audio_stream_count).is_equal_to(2)
    assert_that(header.non_picture_stream_count).is_equal_to(2)
    assert_that(header.probe_score).is_equal_to(100)


def test_header_from_json_cover_art_is_never_a_stream() -> None:
    """An attached picture counts neither as audio nor as a stream (R3)."""
    picture: dict[str, object] = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "mjpeg",
        "disposition": {"default": 1, "attached_pic": 1},
    }

    header = _accepted(probe_json(picture, audio_stream(index=1), format_name="mp3"))

    assert_that(header.stream_index).is_equal_to(1)
    assert_that(header.audio_stream_count).is_equal_to(1)
    assert_that(header.non_picture_stream_count).is_equal_to(1)


def test_header_from_json_probe_score_must_be_integer() -> None:
    """A probe score that is not a JSON integer is not recorded."""
    assert_that(_accepted(probe_json(probe_score="100")).probe_score).is_none()


@pytest.mark.parametrize(
    "data",
    [
        probe_json(format_name="concat"),
        probe_json(format_name="hls"),
        probe_json(format_name="mp4"),
        probe_json(format_name="mov,mp4,m4a,3gp,3g2,mj2,extra"),
        probe_json(format_name=None),
        probe_json({"index": 0, "codec_type": "video", "codec_name": "h264"}),
        probe_json(audio_stream(disposition={"attached_pic": 1})),
        probe_json(audio_stream(codec_name=None)),
        probe_json(audio_stream(codec_name="")),
        probe_json(audio_stream(sample_rate="0")),
        probe_json(audio_stream(sample_rate="N/A")),
        probe_json(audio_stream(channels=0)),
        probe_json(audio_stream(index=None)),
        {"streams": "nope", "format": {"format_name": "flac"}},
        {"streams": [], "format": "nope"},
        {"streams": ["nope"], "format": {"format_name": "flac"}},
    ],
    ids=[
        "concat_container",
        "hls_container",
        "partial_container_name",
        "longer_container_name",
        "no_container",
        "no_audio_stream",
        "audio_is_cover_art",
        "no_codec",
        "empty_codec",
        "zero_rate",
        "missing_rate",
        "zero_channels",
        "no_index",
        "streams_not_list",
        "format_not_object",
        "stream_not_object",
    ],
)
def test_header_from_json_refuses(data: dict[str, object]) -> None:
    """Any failed R2 condition makes every fact unknown."""
    assert_that(header_from_json(data, estimate_warned=False)).is_none()


def test_header_from_json_parses_time_base() -> None:
    """The primary stream's time base is kept in seconds (A5)."""
    data = probe_json(audio_stream(time_base="1/48000"))

    assert_that(_accepted(data).time_base).is_equal_to(Decimal(1) / Decimal(48000))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1/1000", Decimal("0.001")),
        ("3/9", Decimal(3) / Decimal(9)),
        ("0/1", None),
        ("1/0", None),
        ("1/-5", None),
        ("0.001", None),
        ("1/12345678901", None),
        (1000, None),
        (None, None),
    ],
)
def test_parse_time_base(value: object, expected: Decimal | None) -> None:
    """Only a fraction of two positive integers is a time base."""
    assert_that(parse_time_base(value)).is_equal_to(expected)


def test_interpret_header_flags_nonzero_exit() -> None:
    """A failed header call is corrupt even with clean stderr."""
    run = run_result(returncode=1, stdout=json.dumps(probe_json()).encode())

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.CORRUPT,)),
    )


def test_interpret_header_flags_unlisted_stderr() -> None:
    """Exit 0 is not trusted: an unlisted stderr line is unrecognized (A1)."""
    run = run_result(
        stdout=json.dumps(probe_json()).encode(),
        stderr=b"[flac @ 0x1] Packet corrupt (stream = 0, dts = 0).\n",
    )

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.UNRECOGNIZED_OUTPUT,)),
    )


def test_interpret_header_without_pins_refuses_benign_line() -> None:
    """On an unpinned major even a 9.x benign line is unrecognized."""
    run = run_result(
        stdout=json.dumps(probe_json()).encode(),
        stderr=f"[mp3 @ 0x1] {ESTIMATE_LINE}\n".encode(),
    )

    assert_that(interpret_header(run, benign=frozenset())).is_equal_to(
        (None, (AudioFlag.UNRECOGNIZED_OUTPUT,)),
    )


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, json.dumps(probe_json()).encode()), (0, b"not json")],
    ids=["nonzero_exit", "invalid_json"],
)
def test_interpret_header_corrupt_beats_unrecognized(
    returncode: int,
    stdout: bytes,
) -> None:
    """A non-zero exit or invalid JSON stays corrupt, whatever stderr says."""
    run = run_result(
        returncode=returncode,
        stdout=stdout,
        stderr=b"something unexpected\n",
    )

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.CORRUPT,)),
    )


def test_interpret_header_flags_bad_json() -> None:
    """Unparseable stdout is corrupt."""
    run = run_result(stdout=b'{"format": {}, "format": {}}')

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.CORRUPT,)),
    )


def test_interpret_header_records_estimate_line() -> None:
    """A benign estimate line is accepted and remembered for R5."""
    run = run_result(
        stdout=json.dumps(probe_json()).encode(),
        stderr=f"[flac @ 0x1] {ESTIMATE_LINE}\n".encode(),
    )

    header, flags = interpret_header(run, benign=BENIGN_9)

    assert_that(flags).is_empty()
    assert_that(header).is_not_none()
    assert_that(getattr(header, "estimate_warned", None)).is_true()


def test_interpret_header_unknown_without_flag_for_video_only() -> None:
    """A clean file with no audio is unknown but not flagged."""
    data = probe_json({"index": 0, "codec_type": "video", "codec_name": "h264"})

    assert_that(
        interpret_header(run_result(stdout=json.dumps(data).encode()), benign=BENIGN_9),
    ).is_equal_to((None, ()))


# --- packet walk accounting (R6) --------------------------------------------


def test_packet_walk_summarises_lines() -> None:
    """Times, durations, and the lower median are tracked."""
    walk = _walk(
        "-0.006500,0.020000,K__,",
        "0.013500,0.020000,K__",
        "0.033500,0.006500,K__\r",
        "",
    )

    assert_that(walk.packets).is_equal_to(3)
    assert_that(walk.first_time).is_equal_to(Decimal("-0.006500"))
    assert_that(walk.measured_duration()).is_equal_to(Decimal("0.046500"))
    assert_that(walk.median_duration()).is_equal_to(Decimal("0.020000"))


def test_packet_walk_lower_median_for_even_count() -> None:
    """With an even count the lower of the two middle values is used."""
    walk = _walk("0,1,K__", "1,2,K__", "3,3,K__", "6,4,K__")

    assert_that(walk.median_duration()).is_equal_to(Decimal(2))


@pytest.mark.parametrize(
    "line",
    [b"N/A,0.02,K__", b"0.0,N/A,K__", b",0.02,K__", b"0.0,0.02", b"\xe9,1,K__"],
)
def test_packet_walk_missing_time(line: bytes) -> None:
    """A packet without a time or duration, or a malformed line, is noted."""
    walk = PacketWalk()
    walk.feed(line)

    assert_that(walk.packets).is_equal_to(1)
    assert_that(walk.missing_time).is_true()


def test_packet_walk_detects_backwards_time_and_corrupt_flag() -> None:
    """A time lower than the one before and a C flag are both noted."""
    walk = _walk("0.5,0.02,K__", "0.4,0.02,K_C")

    assert_that(walk.backwards).is_true()
    assert_that(walk.corrupt_packet).is_true()


def test_packet_walk_bounds_distinct_durations() -> None:
    """The walk keeps at most a fixed number of distinct durations (A7)."""
    walk = PacketWalk()
    for index in range(MAX_DISTINCT_PACKET_DURATIONS + 10):
        walk.feed(f"{index}.000000,0.{index + 1:06d},K__".encode())

    assert_that(len(walk.durations)).is_equal_to(MAX_DISTINCT_PACKET_DURATIONS)
    assert_that(walk.too_varied).is_true()
    assert_that(walk.packets).is_equal_to(MAX_DISTINCT_PACKET_DURATIONS + 10)


def test_packet_walk_repeated_durations_are_not_too_varied() -> None:
    """Repeating a known duration after the cap is reached is fine."""
    walk = PacketWalk()
    for index in range(MAX_DISTINCT_PACKET_DURATIONS):
        walk.feed(f"{index}.000000,0.{index + 1:06d},K__".encode())
    walk.feed(b"99999.000000,0.000001,K__")

    assert_that(walk.too_varied).is_false()


def test_too_many_distinct_durations_is_inconsistent() -> None:
    """A walk that gave up on its durations reports inconsistent."""
    walk = PacketWalk()
    for index in range(MAX_DISTINCT_PACKET_DURATIONS + 1):
        walk.feed(f"{index}.000000,0.{index + 1:06d},K__".encode())

    assert_that(_decide(walk)).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))


def test_packet_walk_empty() -> None:
    """An empty walk has no measured duration or median."""
    walk = PacketWalk()

    assert_that(walk.measured_duration()).is_none()
    assert_that(walk.median_duration()).is_none()


# --- declared duration (R5) -------------------------------------------------


def test_declared_prefers_stream_duration() -> None:
    """The stream's duration is the declared duration."""
    header = _header(stream_duration=Decimal(5), format_duration=Decimal(9))

    assert_that(
        declared_duration(header, _walk(), estimate_warned=False),
    ).is_equal_to(Decimal(5))


@pytest.mark.parametrize(
    "container",
    ["ogg", "mpegts", "mpeg", "flv", "aac", "ac3", "eac3"],
)
def test_declared_never_for_listed_containers(container: str) -> None:
    """These containers never declare a duration."""
    header = _header(container=container)

    assert_that(declared_duration(header, _walk(), estimate_warned=False)).is_none()


def test_declared_never_after_estimate_line() -> None:
    """An estimate line means nothing is declared."""
    assert_that(declared_duration(_header(), _walk(), estimate_warned=True)).is_none()


def test_declared_from_format_only_with_one_stream() -> None:
    """format.duration counts only when the file has one non-picture stream."""
    single = _header(stream_duration=None, format_duration=Decimal(7))
    several = replace(single, non_picture_stream_count=2)
    walk = _walk(*_packets(100))

    assert_that(
        declared_duration(single, walk, estimate_warned=False),
    ).is_equal_to(Decimal(7))
    assert_that(declared_duration(several, walk, estimate_warned=False)).is_none()


@pytest.mark.parametrize(
    ("start", "declared"),
    [
        ("0.000000", Decimal(7)),
        ("0.020000", Decimal(7)),
        ("-0.020000", Decimal(7)),
        ("0.020001", None),
        ("5.000000", None),
        (None, None),
    ],
    ids=[
        "zero",
        "one_packet",
        "minus_one_packet",
        "over_one_packet",
        "five_seconds",
        "unknown",
    ],
)
def test_format_duration_needs_a_zero_start(
    start: str | None,
    declared: Decimal | None,
) -> None:
    """A4: format.duration is an end time, used only from a zero start."""
    header = _header(
        stream_duration=None,
        format_duration=Decimal(7),
        format_start=None if start is None else Decimal(start),
    )

    assert_that(
        declared_duration(header, _walk(*_packets(100)), estimate_warned=False),
    ).is_equal_to(declared)


def test_stream_duration_does_not_need_a_zero_start() -> None:
    """A4 leaves the stream's own duration alone."""
    header = _header(format_start=Decimal(5), stream_duration=Decimal(2))

    assert_that(
        declared_duration(header, _walk(*_packets(100)), estimate_warned=False),
    ).is_equal_to(Decimal(2))


def test_format_duration_needs_packets() -> None:
    """Without a packet there is no median, so no zero start is shown."""
    header = _header(stream_duration=None, format_duration=Decimal(7))

    assert_that(declared_duration(header, _walk(), estimate_warned=False)).is_none()


def test_offset_matroska_is_estimated() -> None:
    """A4 end to end: audio-only Matroska from 5 s is measured, not truncated."""
    header = _matroska_header(
        non_picture_stream_count=1,
        format_start=Decimal(5),
        format_duration=Decimal(7),
    )

    assert_that(
        _decide(_walk(*_packets(100, start="5")), header=header),
    ).is_equal_to((Decimal("2.000000"), None, (AudioFlag.ESTIMATED,)))


def _matroska_header(**overrides: Any) -> ProbeHeader:
    """Build a two-stream Matroska header without a stream duration.

    Args:
        overrides: Fields to replace.

    Returns:
        The header.
    """
    fields: dict[str, Any] = {
        "container": "matroska,webm",
        "stream_duration": None,
        "format_duration": Decimal("2.008"),
        "non_picture_stream_count": 2,
    }
    fields.update(overrides)
    return _header(**fields)


def test_delayed_matroska_declares_nothing() -> None:
    """A5: delayed Matroska audio with video has no declared duration.

    Its ``DURATION`` tag is never read, so the walk measures the audio.
    """
    walk = _walk(*_packets(50, start="0.994"))

    assert_that(
        declared_duration(_matroska_header(), walk, estimate_warned=False),
    ).is_none()
    assert_that(_decide(walk, header=_matroska_header())).is_equal_to(
        (Decimal("1.000000"), None, (AudioFlag.ESTIMATED,)),
    )


def test_matroska_tag_is_never_read() -> None:
    """A5: a DURATION tag leaves the header facts unchanged."""
    plain = probe_json(audio_stream(duration=None), format_name="matroska,webm")
    tagged = probe_json(
        audio_stream(duration=None, tags={"DURATION": "00:00:02.008000000"}),
        format_name="matroska,webm",
    )

    assert_that(_accepted(tagged)).is_equal_to(_accepted(plain))


def test_audio_only_matroska_uses_format_duration() -> None:
    """One non-picture stream from zero: format.duration is declared (A4)."""
    single = _matroska_header(non_picture_stream_count=1)

    assert_that(
        declared_duration(single, _walk(*_packets(50)), estimate_warned=False),
    ).is_equal_to(Decimal("2.008"))


@pytest.mark.parametrize("container", sorted(AUDIO_ONLY_DECLARED_FORMATS))
def test_avi_and_asf_with_another_stream_declare_nothing(container: str) -> None:
    """A8: any other non-picture stream makes AVI and ASF undeclared."""
    alone = _header(container=container, stream_duration=Decimal("1.5"))
    with_video = replace(alone, non_picture_stream_count=2)
    walk = _walk(*_packets(50))

    assert_that(
        declared_duration(alone, walk, estimate_warned=False),
    ).is_equal_to(Decimal("1.5"))
    assert_that(declared_duration(with_video, walk, estimate_warned=False)).is_none()
    assert_that(_decide(walk, header=with_video)).is_equal_to(
        (Decimal("1.000000"), None, (AudioFlag.ESTIMATED,)),
    )


@pytest.mark.parametrize("container", ["mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"])
def test_other_containers_with_video_keep_the_stream_duration(container: str) -> None:
    """A8 is limited to AVI and ASF: elsewhere the stream's duration stands."""
    header = _header(
        container=container,
        stream_duration=Decimal("1.5"),
        non_picture_stream_count=2,
    )

    assert_that(
        declared_duration(header, _walk(*_packets(50)), estimate_warned=False),
    ).is_equal_to(Decimal("1.5"))


# --- duration decision (R6) -------------------------------------------------


def _decide(
    walk: PacketWalk,
    *,
    header: ProbeHeader | None = None,
    returncode: int = 0,
    stderr: bytes = b"",
) -> tuple[Decimal | None, bool | None, tuple[AudioFlag, ...]]:
    """Run :func:`decide_duration` and unpack its outcome.

    Args:
        walk: The walk.
        header: Header facts; a 2 s FLAC header by default.
        returncode: Walk exit status.
        stderr: Walk stderr.

    Returns:
        Duration, truncation state, and flags.
    """
    outcome = decide_duration(
        header or _header(),
        walk,
        run_result(returncode=returncode, stderr=stderr),
        benign=BENIGN_9,
    )
    return outcome.duration, outcome.truncated, outcome.flags


def test_step1_nonzero_exit_is_corrupt() -> None:
    """Step 1: a failed walk is corrupt."""
    assert_that(_decide(_walk(*_packets(100)), returncode=1)).is_equal_to(
        (None, None, (AudioFlag.CORRUPT,)),
    )


def test_step1_unlisted_stderr_beats_other_steps() -> None:
    """Step 1 wins over steps 2 to 4; the line is unrecognized (A1)."""
    stderr = b"[flac @ 0x1] File ended prematurely\n"

    assert_that(_decide(_walk(), stderr=stderr)).is_equal_to(
        (None, None, (AudioFlag.UNRECOGNIZED_OUTPUT,)),
    )


def test_step1_nonzero_exit_beats_unrecognized_line() -> None:
    """A failed walk is corrupt even when its stderr is also unexpected."""
    stderr = b"[flac @ 0x1] File ended prematurely\n"

    assert_that(
        _decide(_walk(*_packets(100)), returncode=1, stderr=stderr),
    ).is_equal_to((None, None, (AudioFlag.CORRUPT,)))


def test_step2_no_packets_is_truncated() -> None:
    """Step 2: no packets means truncated with no duration."""
    assert_that(_decide(_walk())).is_equal_to((None, True, ()))


def test_step3_missing_time_beats_corrupt_flag() -> None:
    """Step 3 wins over step 4."""
    walk = _walk("0,0.02,K_C", "N/A,0.02,K__")

    assert_that(_decide(walk)).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))


def test_step3_backwards_time_is_inconsistent() -> None:
    """Step 3: a time lower than the one before is inconsistent."""
    walk = _walk(*_packets(50), *_packets(50))

    assert_that(_decide(walk)).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))


def test_step4_corrupt_packet_flag() -> None:
    """Step 4: a packet flagged C is corrupt."""
    lines = _packets(100)
    lines[-1] = lines[-1].replace("K__", "K_C")

    assert_that(_decide(_walk(*lines))).is_equal_to((None, None, (AudioFlag.CORRUPT,)))


def test_step5_estimated_without_declared() -> None:
    """Step 5: no declared duration means measured and estimated."""
    header = _header(container="ogg")

    assert_that(_decide(_walk(*_packets(100)), header=header)).is_equal_to(
        (Decimal("2.000000"), None, (AudioFlag.ESTIMATED,)),
    )


def test_step5_estimate_line_in_walk_stderr() -> None:
    """An estimate line in the walk's stderr also means estimated."""
    stderr = f"[mp3 @ 0x1] {ESTIMATE_LINE}\n".encode()

    assert_that(_decide(_walk(*_packets(100)), stderr=stderr)).is_equal_to(
        (Decimal("2.000000"), None, (AudioFlag.ESTIMATED,)),
    )


def _short_by(shortfall: str) -> PacketWalk:
    """Build a walk of 100 packets whose last one ends ``shortfall`` early.

    Args:
        shortfall: Seconds missing before the 2 s declared duration.

    Returns:
        The walk.
    """
    lines = _packets(99)
    last = Decimal("0.020000") - Decimal(shortfall)
    return _walk(*lines, f"1.980000,{last:.6f},K__")


def test_step6_short_by_one_packet_is_truncated() -> None:
    """A5: one whole 20 ms packet missing is beyond the 5 ms floor."""
    assert_that(_decide(_walk(*_packets(99)))).is_equal_to((None, True, ()))


@pytest.mark.parametrize(
    ("shortfall", "truncated"),
    [("0.005000", False), ("0.005001", True), ("0.000000", False)],
    ids=["at_floor", "over_floor", "exact"],
)
def test_step6_floor_boundary(shortfall: str, truncated: bool) -> None:
    """A5: a shortfall must exceed max(5 ms, two time-base units)."""
    duration, flagged, flags = _decide(_short_by(shortfall))

    assert_that(flagged).is_equal_to(truncated)
    assert_that(flags).is_empty()
    assert_that(duration).is_equal_to(None if truncated else Decimal("2.000000"))


@pytest.mark.parametrize(
    ("time_base", "shortfall", "truncated"),
    [("1/100", "0.020000", False), ("1/100", "0.020001", True)],
    ids=["two_units", "over_two_units"],
)
def test_step6_coarse_time_base_boundary(
    time_base: str,
    shortfall: str,
    truncated: bool,
) -> None:
    """A5: with a coarse time base two units exceed the 5 ms floor."""
    header = _header(time_base=parse_time_base(time_base))

    assert_that(_decide(_short_by(shortfall), header=header)[1]).is_equal_to(
        truncated,
    )


@pytest.mark.parametrize(
    ("time_base", "expected"),
    [
        (None, SHORTFALL_FLOOR_SECONDS),
        ("1/44100", SHORTFALL_FLOOR_SECONDS),
        ("1/1000", SHORTFALL_FLOOR_SECONDS),
        ("1/100", Decimal("0.02")),
        ("1/25", Decimal("0.08")),
    ],
)
def test_shortfall_tolerance(time_base: str | None, expected: Decimal) -> None:
    """The tolerance is max(5 ms, two time-base units), 5 ms without one."""
    header = _header(
        time_base=None if time_base is None else parse_time_base(time_base)
    )

    assert_that(shortfall_tolerance(header)).is_equal_to(expected)


def test_step7_long_by_more_than_three_packets_is_inconsistent() -> None:
    """Step 7: measured more than three packets long is inconsistent."""
    assert_that(_decide(_walk(*_packets(104)))).is_equal_to(
        (None, None, (AudioFlag.INCONSISTENT,)),
    )


def test_step8_reports_declared_duration() -> None:
    """Step 8: within bounds, the declared duration with truncated=false."""
    assert_that(_decide(_walk(*_packets(103)))).is_equal_to(
        (Decimal("2.000000"), False, ()),
    )


def test_decide_inconsistent_when_no_packet_was_timed() -> None:
    """Packets without any usable time give no measurement."""
    walk = PacketWalk(packets=1)

    assert_that(_decide(walk)).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))
