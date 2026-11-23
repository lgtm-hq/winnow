"""Unit tests for the pure audio rules (issue #400), no ffprobe needed.

Hand-built ffprobe output is fed to the functions in
:mod:`winnow.media._audio_rules`: stderr filtering (R2, A1 to A3), strict JSON
(R1), the header outcome (A12a to A12c), stream selection and facts (R2 to
R4, A12f), the declared duration (R5, A5, A8, A9, A9b, A15b), and the
packet-walk decision (R6, A5, A7, A10, A11, A12d, A12e, A15a).
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any, Final

import pytest
from assertpy import assert_that

from tests.media.audio_support import audio_stream, probe_json, run_result
from winnow.media import audio as audio_module
from winnow.media._audio_rules import (
    AUDIO_ONLY_DECLARED_FORMATS,
    COVER_TIMESCALE_WARNING,
    DATA_TRACK_WARNING,
    HE_AAC_PROFILES,
    HEADER_SHOW_ENTRIES,
    MAX_DISTINCT_PACKET_DURATIONS,
    MP4_FORMAT_NAME,
    SHORTFALL_FLOOR_SECONDS,
    StderrContext,
    declared_without_packets,
    first_packet_stretched,
    has_unrecognized_line,
    is_fragmented,
    is_he_aac,
    may_be_primed,
    parse_probe_score,
    parse_time_base,
    program_failed,
    shortfall_tolerance,
    spans_gap,
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


MOOV_FRAME_COUNT = 1_000_000
"""``nb_frames`` of a test header: more than any test walk has, so an MP4
header describes a file whose packets are all indexed in ``moov`` (A15a)."""


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
        frame_count=MOOV_FRAME_COUNT,
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


@pytest.mark.parametrize("returncode", [1, 2, 69, 255])
def test_interpret_header_nonzero_exit_is_unsupported(returncode: int) -> None:
    """A12b: a failed header call is unsupported, never corrupt."""
    run = run_result(returncode=returncode, stdout=json.dumps(probe_json()).encode())

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.UNSUPPORTED,)),
    )


@pytest.mark.parametrize("returncode", [-1, -9, -15, 126, 127])
def test_interpret_header_program_failure_is_backend_unavailable(
    returncode: int,
) -> None:
    """A12c: a signal or exit 126/127 says nothing about the file."""
    run = run_result(returncode=returncode, stdout=json.dumps(probe_json()).encode())

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.BACKEND_UNAVAILABLE,)),
    )


@pytest.mark.parametrize(
    ("returncode", "failed"),
    [(0, False), (1, False), (125, False), (126, True), (127, True), (-9, True)],
)
def test_program_failed(returncode: int, failed: bool) -> None:
    """A12c: only signals and 126/127 count as ffprobe failing to run."""
    assert_that(program_failed(returncode)).is_equal_to(failed)


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


def test_interpret_header_unsupported_beats_unrecognized() -> None:
    """A non-zero exit is unsupported whatever stderr says."""
    run = run_result(
        returncode=1,
        stdout=json.dumps(probe_json()).encode(),
        stderr=b"something unexpected\n",
    )

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.UNSUPPORTED,)),
    )


@pytest.mark.parametrize(
    "stdout",
    [
        b"not json",
        b'{"format": {}, "format": {}}',
        b'{"format": {"format_name": "wav\xe9"}}',
        b"[]",
    ],
    ids=["not_json", "duplicate_key", "not_utf8", "not_an_object"],
)
def test_interpret_header_bad_stdout_is_unrecognized(stdout: bytes) -> None:
    """A12a: stdout ffprobe cannot render says nothing about the file."""
    assert_that(
        interpret_header(run_result(stdout=stdout), benign=BENIGN_9),
    ).is_equal_to((None, (AudioFlag.UNRECOGNIZED_OUTPUT,)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (25, 25),
        (100, 100),
        (101, None),
        (-1, None),
        (2**63, None),
        ("50", None),
        (True, None),
        (50.0, None),
        (None, None),
    ],
)
def test_parse_probe_score(value: object, expected: int | None) -> None:
    """A12f: a probe score is a JSON integer from 0 to 100, else unknown."""
    assert_that(parse_probe_score(value)).is_equal_to(expected)


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


@pytest.mark.parametrize(
    "streams",
    [
        [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        [],
        [
            audio_stream(
                disposition={"default": 0, "attached_pic": 1},
                codec_type="video",
            ),
        ],
    ],
    ids=["video-only", "no-streams", "cover-only"],
)
def test_interpret_header_without_audio_says_no_audio_stream(
    streams: list[dict[str, object]],
) -> None:
    """A13c, A14b: a clean file with no audio stream is unknown and says why.

    An empty M4A or MOV lists no stream at all. A file with no audio stream
    contradicts nothing, so the flag is ``no_audio_stream``, not
    ``inconsistent``.
    """
    data = {**probe_json(format_name=MP4_FORMAT_NAME), "streams": streams}

    assert_that(
        interpret_header(run_result(stdout=json.dumps(data).encode()), benign=BENIGN_9),
    ).is_equal_to((None, (AudioFlag.NO_AUDIO_STREAM,)))


@pytest.mark.parametrize(
    "data",
    [
        {"streams": [audio_stream()]},
        {"format": {"format_name": "flac"}},
        {"format": [], "streams": [audio_stream()]},
        {"format": {"format_name": "flac"}, "streams": {}},
        {"format": {"format_name": "flac"}, "streams": [audio_stream(), 3]},
        probe_json(audio_stream(index="0")),
        probe_json(audio_stream(index=-1)),
        probe_json(audio_stream(index=True)),
        {"format": {"duration": "2.0"}, "streams": [audio_stream()]},
        probe_json(format_name=3),
        probe_json(format_name=None),
    ],
    ids=[
        "no-format",
        "no-streams-key",
        "format-not-object",
        "streams-not-list",
        "stream-not-object",
        "string-index",
        "negative-index",
        "bool-index",
        "no-format-name",
        "number-format-name",
        "null-format-name",
    ],
)
def test_interpret_header_malformed_shape_is_unrecognized(
    data: dict[str, object],
) -> None:
    """A14b: JSON without the shape ffprobe writes is ``unrecognized_output``.

    A missing or non-string ``format_name`` is a shape fault too (A15d); a
    string off the allow-list stays ``inconsistent``.
    """
    assert_that(
        interpret_header(run_result(stdout=json.dumps(data).encode()), benign=BENIGN_9),
    ).is_equal_to((None, (AudioFlag.UNRECOGNIZED_OUTPUT,)))


@pytest.mark.parametrize(
    "data",
    [
        probe_json(audio_stream(codec_name=None)),
        probe_json(audio_stream(codec_name="")),
        probe_json(audio_stream(sample_rate="0")),
        probe_json(audio_stream(sample_rate=None)),
        probe_json(audio_stream(channels=None)),
        probe_json(format_name="au"),
    ],
    ids=[
        "no-codec",
        "empty-codec",
        "zero-rate",
        "no-rate",
        "no-channels",
        "container-off-list",
    ],
)
def test_interpret_header_unreadable_audio_stays_inconsistent(
    data: dict[str, object],
) -> None:
    """A14b: an audio stream without a codec or sample rate stays ``inconsistent``.

    So does a container off the allow-list in a clean header, which the
    ``-format_whitelist`` the reader passes should never let through.
    """
    assert_that(
        interpret_header(run_result(stdout=json.dumps(data).encode()), benign=BENIGN_9),
    ).is_equal_to((None, (AudioFlag.INCONSISTENT,)))


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
        declared_duration(header, _walk(*_packets(100)), estimate_warned=False),
    ).is_equal_to(Decimal(5))


@pytest.mark.parametrize(
    "container",
    ["ogg", "mpegts", "mpeg", "flv", "aac", "ac3", "eac3", "avi"],
)
def test_declared_never_for_listed_containers(container: str) -> None:
    """These containers never declare a duration, even with healthy packets.

    The same header and walk declare 2 s as ``flac``, so only the container
    guard makes the difference.
    """
    walk = _walk(*_packets(100))

    assert_that(
        declared_duration(_header(), walk, estimate_warned=False),
    ).is_equal_to(Decimal("2.000000"))
    assert_that(
        declared_duration(_header(container=container), walk, estimate_warned=False),
    ).is_none()


def test_declared_never_after_estimate_line() -> None:
    """An estimate line means nothing is declared, even with healthy packets."""
    walk = _walk(*_packets(100))

    assert_that(
        declared_duration(_header(), walk, estimate_warned=False),
    ).is_equal_to(Decimal("2.000000"))
    assert_that(declared_duration(_header(), walk, estimate_warned=True)).is_none()


def test_declared_from_format_only_with_one_stream() -> None:
    """format.duration counts only when the file has one non-picture stream."""
    single = _header(stream_duration=None, format_duration=Decimal(7))
    several = replace(single, non_picture_stream_count=2)
    walk = _walk(*_packets(100))

    assert_that(
        declared_duration(single, walk, estimate_warned=False),
    ).is_equal_to(Decimal(7))
    assert_that(declared_duration(several, walk, estimate_warned=False)).is_none()


# Two time-base units of the default 1/44100 header: 45.35 microseconds.
@pytest.mark.parametrize(
    ("start", "declared"),
    [
        ("0.000000", Decimal(7)),
        ("0.000045", Decimal(7)),
        ("-0.005000", Decimal(7)),
        ("-0.050000", Decimal(7)),
        ("0.000046", None),
        ("0.005000", None),
        ("0.020000", None),
        ("5.000000", None),
        (None, None),
    ],
    ids=[
        "zero",
        "two_units",
        "negative",
        "negative_priming",
        "over_two_units",
        "five_ms",
        "one_packet",
        "five_seconds",
        "unknown",
    ],
)
def test_format_duration_needs_a_zero_start(
    start: str | None,
    declared: Decimal | None,
) -> None:
    """A9: format.duration needs format.start_time within two time-base units."""
    header = _header(
        stream_duration=None,
        format_duration=Decimal(7),
        format_start=None if start is None else Decimal(start),
    )

    assert_that(
        declared_duration(header, _walk(*_packets(100)), estimate_warned=False),
    ).is_equal_to(declared)


@pytest.mark.parametrize(
    ("first", "declared"),
    [
        ("0", Decimal(2)),
        ("0.000045", Decimal(2)),
        ("-0.025057", Decimal(2)),
        ("0.000046", None),
        ("0.005", None),
        ("0.978667", None),
        ("5", None),
    ],
    ids=[
        "zero",
        "two_units",
        "priming",
        "over_two_units",
        "five_ms",
        "delayed",
        "five_seconds",
    ],
)
def test_stream_duration_needs_a_first_packet_near_zero(
    first: str,
    declared: Decimal | None,
) -> None:
    """A9: a stream whose first packet is late declares nothing."""
    header = _header(stream_duration=Decimal(2))

    assert_that(
        declared_duration(
            header,
            _walk(*_packets(100, start=first)),
            estimate_warned=False,
        ),
    ).is_equal_to(declared)


def test_format_duration_also_needs_the_first_packet_near_zero() -> None:
    """A9: a zero format start does not help a stream that starts late."""
    header = _header(stream_duration=None, format_duration=Decimal(7))

    assert_that(
        declared_duration(
            header,
            _walk(*_packets(100, start="0.005")),
            estimate_warned=False,
        ),
    ).is_none()


def test_start_bound_needs_a_time_base() -> None:
    """A9: without a time base no start is established, nothing is declared."""
    header = _header(time_base=None)

    assert_that(
        declared_duration(header, _walk(*_packets(100)), estimate_warned=False),
    ).is_none()


def test_stream_duration_does_not_need_a_zero_format_start() -> None:
    """The stream's own duration depends on its first packet, not the format."""
    header = _header(format_start=Decimal(5), stream_duration=Decimal(2))

    assert_that(
        declared_duration(header, _walk(*_packets(100)), estimate_warned=False),
    ).is_equal_to(Decimal(2))


def test_pcm_matroska_five_ms_late_is_estimated() -> None:
    """A9 end to end: PCM Matroska that starts 5 ms late is never truncated.

    Its end time 1.375 minus the start leaves the measured 1.369 s 6 ms short,
    which A5 would call truncated if the end time were declared.
    """
    header = _header(
        container="matroska,webm",
        codec="pcm_s16le",
        sample_rate=48000,
        channels=1,
        time_base=Decimal("0.001"),
        stream_duration=None,
        format_duration=Decimal("1.375"),
        format_start=Decimal("0.005"),
    )
    lines = [*_packets(65, step="0.021000", start="0.005"), "1.370000,0.004000,K__"]

    assert_that(_decide(_walk(*lines), header=header)).is_equal_to(
        (Decimal("1.369000"), None, (AudioFlag.ESTIMATED,)),
    )


@pytest.mark.parametrize("container", ["avi", "asf"])
def test_delayed_audio_only_avi_and_asf_are_estimated(container: str) -> None:
    """A9 and A9b: audio-only AVI and ASF delayed by 5 s declare nothing.

    Their stream duration is the end of the file's timeline, 6.384 s, for
    1.37 s of audio.
    """
    header = _header(
        container=container,
        time_base=Decimal(1) / Decimal(48000),
        stream_duration=Decimal("6.384"),
        format_duration=Decimal("6.384"),
    )
    walk = _walk(*_packets(59, step="0.024000", start="5"))

    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.416000"), None, (AudioFlag.ESTIMATED,)),
    )


def test_format_duration_needs_packets() -> None:
    """Without a packet nothing confirms the stream, so nothing is declared."""
    header = _header(stream_duration=None, format_duration=Decimal(7))

    assert_that(declared_duration(header, _walk(), estimate_warned=False)).is_none()


def test_offset_matroska_is_estimated() -> None:
    """A9 end to end: audio-only Matroska from 5 s is measured, not truncated."""
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
    """One non-picture stream from zero: format.duration is declared (A9)."""
    single = _matroska_header(non_picture_stream_count=1)

    assert_that(
        declared_duration(single, _walk(*_packets(50)), estimate_warned=False),
    ).is_equal_to(Decimal("2.008"))


def test_avi_never_declares_a_duration() -> None:
    """A9b: AVI declares nothing, even audio alone with packets from zero.

    Delayed audio-only AVI keeps its packets at 0 and puts the delay in the
    stream's duration field, so the field is not the audio's length.
    """
    header = _header(container="avi", stream_duration=Decimal("1.488"))
    walk = _walk(*_packets(50))

    assert_that(declared_duration(header, walk, estimate_warned=False)).is_none()
    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.000000"), None, (AudioFlag.ESTIMATED,)),
    )


@pytest.mark.parametrize("container", sorted(AUDIO_ONLY_DECLARED_FORMATS))
def test_asf_with_another_stream_declares_nothing(container: str) -> None:
    """A8: any other non-picture stream makes ASF undeclared.

    AVI is not here: it never declares a duration at all (A9b).
    """
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
    """A8 is limited to ASF (AVI declares nothing, A9b): elsewhere it stands."""
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
    """Step 2: no packets against a positive declared duration is truncated."""
    assert_that(_decide(_walk())).is_equal_to((None, True, ()))


@pytest.mark.parametrize(
    "header",
    [
        _header(stream_duration=None, format_duration=None),
        _header(stream_duration=None, format_duration=None, container="aiff"),
        _header(container="ogg"),
        _header(container="avi"),
        _header(estimate_warned=True),
        _header(container="asf", non_picture_stream_count=2),
        _header(stream_duration=None, non_picture_stream_count=2),
        _header(stream_duration=None, format_start=Decimal(5)),
    ],
    ids=[
        "nothing_declared",
        "empty_aiff",
        "undeclared_container",
        "avi",
        "estimate_line",
        "asf_with_video",
        "format_duration_two_streams",
        "format_duration_late_start",
    ],
)
def test_step2_no_packets_without_declared_is_inconsistent(
    header: ProbeHeader,
) -> None:
    """A12d: an empty walk without a positive declared duration is not damage.

    ffmpeg writes empty FLAC (no duration) and AIFF (duration 0) files.
    """
    assert_that(_decide(_walk(), header=header)).is_equal_to(
        (None, None, (AudioFlag.INCONSISTENT,)),
    )


def test_declared_without_packets_skips_only_the_first_packet_bound() -> None:
    """A12d: with no packet the A9 first-packet bound has nothing to hold."""
    assert_that(
        declared_without_packets(_header(), estimate_warned=False),
    ).is_equal_to(Decimal("2.000000"))
    assert_that(
        declared_without_packets(
            _header(stream_duration=None, format_duration=Decimal(7)),
            estimate_warned=False,
        ),
    ).is_equal_to(Decimal(7))
    assert_that(declared_duration(_header(), _walk(), estimate_warned=False)).is_none()


def test_step2_estimate_line_in_walk_stderr_is_inconsistent() -> None:
    """A12d: an estimate line in the walk's stderr also leaves nothing declared."""
    stderr = f"[mp3 @ 0x1] {ESTIMATE_LINE}\n".encode()

    assert_that(_decide(_walk(), stderr=stderr)).is_equal_to(
        (None, None, (AudioFlag.INCONSISTENT,)),
    )


@pytest.mark.parametrize("returncode", [-9, 126, 127])
def test_step1_program_failure_is_backend_unavailable(returncode: int) -> None:
    """A12c: a walk killed by a signal or not run is not corrupt."""
    assert_that(_decide(_walk(*_packets(100)), returncode=returncode)).is_equal_to(
        (None, None, (AudioFlag.BACKEND_UNAVAILABLE,)),
    )


@pytest.mark.parametrize(
    "lines",
    [
        ("0.000000,-0.020000,K__",),
        (*_packets(99), "1.980000,-0.000001,K__"),
    ],
    ids=["only", "last"],
)
def test_negative_packet_duration_is_missing_time(lines: tuple[str, ...]) -> None:
    """A12e: a negative packet duration counts as a packet without a time."""
    walk = _walk(*lines)

    assert_that(walk.missing_time).is_true()
    assert_that(_decide(walk)).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))


@pytest.mark.parametrize(
    "lines",
    [
        ("0.000000,0.000000,K__", "0.000000,0.000000,K__"),
        ("0.000000,0.000000,K__", "0.000000,0.000000,K__", "0.000000,0.020000,K__"),
        ("1.000000,0.000000,K__",),
    ],
    ids=["all_zero", "zero_median", "single_zero"],
)
def test_non_positive_measure_is_inconsistent(lines: tuple[str, ...]) -> None:
    """A12e: a measured span or median that is not positive is no duration."""
    for header in (_header(), _header(container="ogg")):
        assert_that(_decide(_walk(*lines), header=header)).is_equal_to(
            (None, None, (AudioFlag.INCONSISTENT,)),
        )


def test_step3_missing_time_beats_corrupt_flag() -> None:
    """Step 3 wins over step 4."""
    walk = _walk("0,0.02,K_C", "N/A,0.02,K__")

    assert_that(_decide(walk)).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))


def test_step3_backwards_time_is_inconsistent() -> None:
    """Step 3: a time lower than the one before is inconsistent."""
    walk = _walk(*_packets(50), *_packets(50))

    assert_that(_decide(walk)).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))


def _gap_walk(first: str, *, count: int = 60, step: str = "0.021333") -> PacketWalk:
    """Build a walk whose first packet lasts ``first`` seconds.

    Args:
        first: Duration of the first packet.
        count: Packets after the first.
        step: Duration of every later packet.

    Returns:
        The walk.
    """
    head = f"0.000000,{first},K__"
    return _walk(head, *_packets(count, step=step, start=first))


def _mp4_header(**overrides: Any) -> ProbeHeader:
    """Build an MP4 header for the stretched-packet rule.

    Args:
        overrides: Fields to replace.

    Returns:
        The header.
    """
    return _header(container=MP4_FORMAT_NAME, **overrides)


@pytest.mark.parametrize("first", ["5.000000", "0.100000"], ids=["5s", "100ms"])
def test_gap_packet_is_inconsistent(first: str) -> None:
    """A10b: delayed fragmented MP4 stretches its first packet across the delay.

    The first AAC packet runs from 0 to the end of the delay; the span is
    not evidence of the audio's length, declared duration or not. A 0.1 s
    delay is caught: there is no floor in seconds.
    """
    walk = _gap_walk(first)

    assert_that(spans_gap(walk)).is_true()
    assert_that(_decide(walk, header=_mp4_header(stream_duration=None))).is_equal_to(
        (None, None, (AudioFlag.INCONSISTENT,)),
    )
    assert_that(
        _decide(walk, header=_mp4_header(stream_duration=Decimal("6.28"))),
    ).is_equal_to((None, None, (AudioFlag.INCONSISTENT,)))


@pytest.mark.parametrize(
    ("first", "step", "gap"),
    [
        ("0.042666", "0.021333", False),
        ("0.042667", "0.021333", True),
        ("0.021333", "0.021333", False),
    ],
    ids=["two_medians", "over_two_medians", "uniform"],
)
def test_gap_packet_needs_two_medians(first: str, step: str, gap: bool) -> None:
    """A10b: a packet must exceed twice the median packet duration."""
    assert_that(spans_gap(_gap_walk(first, step=step))).is_equal_to(gap)


@pytest.mark.parametrize("container", ["matroska,webm", "ogg", "asf", "flac"])
def test_gap_packet_is_ignored_outside_mp4(container: str) -> None:
    """A10b applies to MP4 only: other codecs vary packet size by design."""
    walk = _gap_walk("0.100000")
    header = _header(container=container, stream_duration=None)

    assert_that(_decide(walk, header=header)[2]).does_not_contain(
        AudioFlag.INCONSISTENT,
    )


def test_short_last_packet_is_not_a_gap() -> None:
    """A10b: MP4 audio's shorter last packet never trips the bound."""
    walk = _walk(*_packets(60, step="0.021333"), "1.279980,0.005000,K__")

    assert_that(spans_gap(walk)).is_false()


def test_gap_packet_beats_corrupt_flag() -> None:
    """A10b is checked after step 3 and before step 4."""
    walk = _walk("0.000000,5.000000,K_C", *_packets(60, start="5"))

    assert_that(_decide(walk, header=_mp4_header())).is_equal_to(
        (None, None, (AudioFlag.INCONSISTENT,)),
    )


def test_gap_check_needs_durations() -> None:
    """A walk without packet durations has nothing to compare."""
    assert_that(spans_gap(PacketWalk())).is_false()


def test_flv_untimed_packet_makes_every_fact_unknown() -> None:
    """A11: in FLV a packet without a time makes every audio fact unknown."""
    walk = _walk("N/A,N/A,___", "0.000000,N/A,K__", "0.023000,N/A,K__")
    outcome = decide_duration(
        _header(container="flv"),
        walk,
        run_result(),
        benign=BENIGN_9,
    )

    assert_that(outcome.flags).is_equal_to((AudioFlag.INCONSISTENT,))
    assert_that(outcome.facts_unknown).is_true()
    assert_that(outcome.duration).is_none()
    assert_that(outcome.truncated).is_none()


def test_untimed_packet_elsewhere_keeps_the_facts() -> None:
    """A11 is limited to FLV: elsewhere only the duration is unknown."""
    walk = _walk("N/A,N/A,___", "0.000000,0.02,K__")
    outcome = decide_duration(_header(), walk, run_result(), benign=BENIGN_9)

    assert_that(outcome.flags).is_equal_to((AudioFlag.INCONSISTENT,))
    assert_that(outcome.facts_unknown).is_false()


def test_flv_backwards_time_keeps_the_facts() -> None:
    """A11 covers an untimed packet only, not other step 3 findings."""
    walk = _walk(*_packets(50), *_packets(50))
    outcome = decide_duration(
        _header(container="flv"),
        walk,
        run_result(),
        benign=BENIGN_9,
    )

    assert_that(outcome.flags).is_equal_to((AudioFlag.INCONSISTENT,))
    assert_that(outcome.facts_unknown).is_false()


def test_flv_failed_walk_is_corrupt_first() -> None:
    """Step 1 still wins over A11."""
    walk = _walk("N/A,N/A,___", "0.000000,N/A,K__")
    outcome = decide_duration(
        _header(container="flv"),
        walk,
        run_result(returncode=1),
        benign=BENIGN_9,
    )

    assert_that(outcome.flags).is_equal_to((AudioFlag.CORRUPT,))
    assert_that(outcome.facts_unknown).is_false()


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
    """Build a 2 s walk of 99 packets whose last (40 ms) ends ``shortfall`` early.

    The last packet is two packets long, so a shortfall up to 40 ms leaves it
    a positive duration (A12e) while the median stays 20 ms.

    Args:
        shortfall: Seconds missing before the 2 s declared duration.

    Returns:
        The walk.
    """
    lines = _packets(98)
    last = Decimal("0.040000") - Decimal(shortfall)
    return _walk(*lines, f"1.960000,{last:.6f},K__")


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


def test_small_start_offset_is_estimated_not_truncated() -> None:
    """A healthy file starting 10 ms late is measured, never called cut short."""
    header = _matroska_header(
        non_picture_stream_count=1,
        format_start=Decimal("0.010"),
        format_duration=Decimal("2.010"),
    )

    assert_that(
        _decide(_walk(*_packets(100, start="0.010")), header=header),
    ).is_equal_to((Decimal("2.000000"), None, (AudioFlag.ESTIMATED,)))


# --- priming counted as audio (A12h) ------------------------------------------


@pytest.mark.parametrize("codec", ["aac", "mp3"])
def test_primed_codecs_in_matroska_are_estimated(codec: str) -> None:
    """A12h: Matroska AAC/MP3 duration counts the priming, so it is measured.

    Audio-only Matroska from ffmpeg: the first packet starts at -21 ms and
    ``format.duration`` 1.021 is measured from there for 1.000 s of audio.
    """
    header = _matroska_header(
        codec=codec,
        non_picture_stream_count=1,
        time_base=Decimal("0.001"),
        format_duration=Decimal("1.021"),
    )
    walk = _walk(*_packets(48, step="0.021000", start="-0.021"), "0.987,0.013,K__")

    assert_that(declared_duration(header, walk, estimate_warned=False)).is_none()
    assert_that(
        declared_without_packets(header, estimate_warned=False),
    ).is_none()
    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.021000"), None, (AudioFlag.ESTIMATED,)),
    )


@pytest.mark.parametrize("codec", ["opus", "flac", "pcm_s16le"])
def test_other_codecs_in_matroska_still_declare(codec: str) -> None:
    """A12h is limited to AAC and MP3 in Matroska."""
    header = _matroska_header(codec=codec, non_picture_stream_count=1)

    assert_that(
        declared_duration(header, _walk(*_packets(50)), estimate_warned=False),
    ).is_equal_to(Decimal("2.008"))


def test_fragmented_mp4_aac_is_estimated() -> None:
    """A12h: MP4 AAC from zero has no edit list; its duration counts priming.

    Fragmented MP4 at 8 kHz: stream duration 1.128 for 1.000 s of audio, the
    first packet at 0 instead of -0.128.
    """
    header = _mp4_header(
        codec="aac",
        sample_rate=8000,
        time_base=Decimal(1) / Decimal(8000),
        stream_duration=Decimal("1.128"),
    )
    walk = _walk(*_packets(8, step="0.128000"), "1.024000,0.104000,K__")

    assert_that(declared_duration(header, walk, estimate_warned=False)).is_none()
    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.128000"), None, (AudioFlag.ESTIMATED,)),
    )


def test_mp4_aac_with_trimmed_priming_declares() -> None:
    """A12h: an edit list puts the first packet before zero; the duration stands."""
    header = _mp4_header(
        codec="aac",
        sample_rate=8000,
        time_base=Decimal(1) / Decimal(8000),
        stream_duration=Decimal("1.000"),
    )
    walk = _walk(*_packets(9, step="0.128000", start="-0.128"), "1.024,0.104,K__")

    assert_that(
        declared_duration(header, walk, estimate_warned=False),
    ).is_equal_to(Decimal("1.000"))


@pytest.mark.parametrize(
    "codec",
    ["pcm_s16le", "pcm_f32be", "flac", "alac", "wavpack", "tta", "truehd", "mlp"],
)
def test_pcm_and_lossless_mp4_codecs_from_zero_still_declare(codec: str) -> None:
    """A14a: PCM and lossless codecs carry no priming, so they keep checking."""
    header = _mp4_header(codec=codec, stream_duration=Decimal(2))

    assert_that(may_be_primed(codec)).is_false()
    assert_that(
        declared_duration(header, _walk(*_packets(100)), estimate_warned=False),
    ).is_equal_to(Decimal(2))


LOSSY_MP4_CODECS: Final[tuple[str, ...]] = (
    "aac",
    "mp3",
    "mp2",
    "ac3",
    "eac3",
    "opus",
    "vorbis",
    "wmav2",
    "dts",
)
"""A sample of codecs A14a treats as primed: everything but PCM and lossless."""


@pytest.mark.parametrize("codec", LOSSY_MP4_CODECS)
def test_mp4_from_zero_declared_equal_to_span_is_estimated(codec: str) -> None:
    """A14a: from zero, a declared duration about equal to the span declares nothing.

    Fragmented MP4 or ``-use_editlist 0``: the packets hold the priming, so a
    declared duration equal to their span counts it as audio (Vorbis up to
    134 ms, Opus 6.5 ms). The span is the estimate.
    """
    header = _mp4_header(
        codec=codec,
        time_base=Decimal(1) / Decimal(32000),
        stream_duration=Decimal("1.536"),
    )
    walk = _walk(*_packets(48, step="0.032000"))

    assert_that(may_be_primed(codec)).is_true()
    assert_that(declared_duration(header, walk, estimate_warned=False)).is_none()
    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.536000"), None, (AudioFlag.ESTIMATED,)),
    )


@pytest.mark.parametrize("codec", LOSSY_MP4_CODECS)
def test_mp4_from_zero_cut_stays_truncated(codec: str) -> None:
    """A14a: a declared duration above the span by more than tolerance stands.

    Three 32 ms packets cut off a 1.536 s file leave a 1.440 s span; the
    declared 1.536 is kept and the truncation check calls it cut short.
    """
    header = _mp4_header(
        codec=codec,
        time_base=Decimal(1) / Decimal(32000),
        stream_duration=Decimal("1.536"),
    )
    walk = _walk(*_packets(45, step="0.032000"))

    assert_that(
        declared_duration(header, walk, estimate_warned=False),
    ).is_equal_to(Decimal("1.536"))
    assert_that(_decide(walk, header=header)).is_equal_to((None, True, ()))


def test_mp4_from_zero_declared_below_span_is_estimated() -> None:
    """A14a ruling: a declared duration below the span declares nothing.

    MP3 with ``-use_editlist 0``: the packets run about one packet past the
    declared duration, which still counts the priming.
    """
    header = _mp4_header(
        codec="mp3",
        sample_rate=48000,
        time_base=Decimal(1) / Decimal(48000),
        stream_duration=Decimal("1.392"),
    )
    walk = _walk(*_packets(59, step="0.024000"))

    assert_that(declared_duration(header, walk, estimate_warned=False)).is_none()
    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.416000"), None, (AudioFlag.ESTIMATED,)),
    )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [("1.541", None), ("1.542", Decimal("1.542"))],
    ids=["within-tolerance", "beyond-tolerance"],
)
def test_mp4_from_zero_tolerance_boundary(
    declared: str,
    expected: Decimal | None,
) -> None:
    """A14a: only a declared duration more than the A5 tolerance above the span stands.

    The span is 1.536 and the tolerance 5 ms at 32 kHz.
    """
    header = _mp4_header(
        codec="aac",
        time_base=Decimal(1) / Decimal(32000),
        stream_duration=Decimal(declared),
    )
    walk = _walk(*_packets(48, step="0.032000"))

    assert_that(
        declared_duration(header, walk, estimate_warned=False),
    ).is_equal_to(expected)


def test_from_zero_rule_is_limited_to_mp4() -> None:
    """A14a: in Matroska a stream from zero whose declared equals the span declares."""
    header = _matroska_header(
        codec="vorbis",
        non_picture_stream_count=1,
        time_base=Decimal("0.001"),
        stream_duration=Decimal("1.536"),
    )
    walk = _walk(*_packets(48, step="0.032000"))

    assert_that(
        declared_duration(header, walk, estimate_warned=False),
    ).is_equal_to(Decimal("1.536"))


def test_fragmented_mp4_mp3_is_estimated() -> None:
    """A13a: MP3 in fragmented MP4 starts at zero and counts its priming.

    With no edit list the stream duration counts the 1105 LAME priming
    samples as audio, so the packet span is measured instead.
    """
    header = _mp4_header(
        codec="mp3",
        sample_rate=16000,
        time_base=Decimal(1) / Decimal(16000),
        stream_duration=Decimal("1.0800"),
    )
    walk = _walk(*_packets(30, step="0.036000"))

    assert_that(declared_duration(header, walk, estimate_warned=False)).is_none()
    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.080000"), None, (AudioFlag.ESTIMATED,)),
    )


# --- priming recognised by the packet span (A13a revised) ---------------------


def _primed_walk(*, count: int = 48) -> PacketWalk:
    """Build a walk that starts 32 ms before zero, 32 ms packets.

    Args:
        count: Number of packets.

    Returns:
        The walk; its span is ``count`` times 32 ms.
    """
    return _walk(*_packets(count, step="0.032000", start="-0.032"))


@pytest.mark.parametrize(
    "header",
    [
        _header(
            container="matroska,webm",
            codec=codec,
            time_base=Decimal("0.001"),
            stream_duration=None,
            format_duration=Decimal("1.536"),
            format_start=Decimal("-0.032"),
        )
        for codec in ("wmav2", "mp2", "ac3", "eac3", "vorbis", "opus")
    ]
    + [
        _header(
            container=MP4_FORMAT_NAME,
            codec=codec,
            time_base=Decimal(1) / Decimal(32000),
            stream_duration=Decimal("1.536"),
        )
        for codec in ("aac", "mp3", "ac3")
    ],
    ids=lambda header: f"{header.container.split(',')[0]}-{header.codec}",
)
def test_declared_packet_span_counts_priming(header: ProbeHeader) -> None:
    """A13a: a declared duration equal to a span that starts before zero.

    The declared 1.536 equals the span of 48 packets of 32 ms from -0.032
    and runs 32 ms past the last packet's end: it counts the priming, so
    nothing is declared and the span is the estimate.
    """
    walk = _primed_walk()

    assert_that(declared_duration(header, walk, estimate_warned=False)).is_none()
    assert_that(_decide(walk, header=header)).is_equal_to(
        (Decimal("1.536000"), None, (AudioFlag.ESTIMATED,)),
    )


@pytest.mark.parametrize("container", ["matroska,webm", MP4_FORMAT_NAME])
def test_cut_primed_file_stays_truncated(container: str) -> None:
    """A13a: a cut shortens the span, not the declared duration."""
    header = _header(
        container=container,
        codec="ac3",
        time_base=Decimal("0.001"),
        stream_duration=Decimal("1.536"),
    )

    assert_that(_decide(_primed_walk(count=45), header=header)).is_equal_to(
        (None, True, ()),
    )


def test_trimmed_priming_keeps_its_declared_duration() -> None:
    """A13a: an edit list makes the declared duration shorter than the span.

    Faststart AAC: first packet at -0.032, declared 1.504, the last packet's
    end; the span is 1.536, so the rule does not match.
    """
    header = _mp4_header(
        codec="aac",
        time_base=Decimal(1) / Decimal(32000),
        stream_duration=Decimal("1.504"),
    )

    assert_that(_decide(_primed_walk(), header=header)).is_equal_to(
        (Decimal("1.504"), False, ()),
    )


def test_span_rule_needs_declared_past_last_end() -> None:
    """A13a: a start before zero within tolerance does not count as priming.

    First packet at -0.002: declared equals the span and runs only 2 ms
    past the last packet's end, inside the 5 ms tolerance.
    """
    header = _matroska_header(
        codec="ac3",
        non_picture_stream_count=1,
        time_base=Decimal("0.001"),
        format_duration=Decimal("1.000"),
        format_start=Decimal("-0.002"),
    )
    walk = _walk(*_packets(50, step="0.020000", start="-0.002"))

    assert_that(
        declared_duration(header, walk, estimate_warned=False),
    ).is_equal_to(Decimal("1.000"))


def test_span_rule_is_limited_to_matroska_and_mp4() -> None:
    """A13a: in other containers a span starting before zero still declares."""
    header = _header(
        container="asf",
        codec="wmav2",
        time_base=Decimal("0.001"),
        stream_duration=Decimal("1.536"),
    )

    assert_that(
        declared_duration(header, _primed_walk(), estimate_warned=False),
    ).is_equal_to(Decimal("1.536"))


# --- A15: fragmented MP4, HE-AAC, stretched first packet ---------------------


def test_header_asks_for_frame_count_and_profile() -> None:
    """A15a, A15b: the header probe asks for ``nb_frames`` and ``profile``."""
    stream_fields = HEADER_SHOW_ENTRIES.split(":")[1].split(",")

    assert_that(stream_fields).contains("nb_frames", "profile")
    assert_that(HEADER_SHOW_ENTRIES).does_not_contain("tags")


@pytest.mark.parametrize(
    ("overrides", "frame_count", "profile"),
    [
        ({"nb_frames": "47", "profile": "HE-AAC"}, 47, "HE-AAC"),
        ({}, None, None),
        ({"nb_frames": "0", "profile": ""}, None, None),
        ({"nb_frames": "N/A", "profile": 4}, None, None),
    ],
    ids=["reported", "absent", "zero-and-empty", "unparsable"],
)
def test_header_reads_frame_count_and_profile(
    overrides: dict[str, object],
    frame_count: int | None,
    profile: str | None,
) -> None:
    """``nb_frames`` must be a positive integer and ``profile`` a non-empty string."""
    header = _accepted(probe_json(audio_stream(**overrides)))

    assert_that((header.frame_count, header.profile)).is_equal_to(
        (frame_count, profile),
    )


@pytest.mark.parametrize(
    ("container", "frame_count", "packets", "expected"),
    [
        (MP4_FORMAT_NAME, None, 4, True),
        (MP4_FORMAT_NAME, 6, 47, True),
        (MP4_FORMAT_NAME, 47, 47, False),
        (MP4_FORMAT_NAME, 41454, 47, False),
        (MP4_FORMAT_NAME, 192000, 47, False),
        (MP4_FORMAT_NAME, 24576, 47, True),
        (MP4_FORMAT_NAME, 41234, 47, False),
        (MP4_FORMAT_NAME, 41233, 47, True),
        (MP4_FORMAT_NAME, 192000, 0, False),
        (MP4_FORMAT_NAME, None, 0, False),
        ("matroska,webm", None, 47, False),
    ],
    ids=[
        "empty-moov",
        "first-fragment-in-moov",
        "all-in-moov",
        "pcm-samples-all-in-moov",
        "pcm-samples-cut",
        "pcm-samples-first-fragment-in-moov",
        "pcm-samples-within-tolerance",
        "pcm-samples-past-tolerance",
        "pcm-samples-no-packets",
        "no-packets",
        "not-mp4",
    ],
)
def test_is_fragmented(
    container: str,
    frame_count: int | None,
    packets: int,
    expected: bool,
) -> None:
    """A15a: an MP4 walk with more packets than ``nb_frames`` read fragments.

    ``nb_frames`` counts only the samples ``moov`` indexes: none with
    ``empty_moov``, the first fragment's without it. For PCM and ADPCM it
    counts audio samples, so it exceeds the packet count; then the walk's
    span in samples is compared instead (A15f-1). The 47 packets of 20 ms
    span 0.94 s, 41,454 samples at 44.1 kHz, and the 5 ms tolerance is
    220.5 samples. A cut file indexes more samples than its span holds and
    is not fragmented.
    """
    header = _header(container=container, frame_count=frame_count)
    walk = _walk(*_packets(packets))

    assert_that(is_fragmented(header, walk)).is_equal_to(expected)


def _fragmented_header(**overrides: Any) -> ProbeHeader:
    """Build a fragmented ALAC MP4 header: ``nb_frames`` is not reported.

    Args:
        overrides: Fields to replace.

    Returns:
        The header.
    """
    fields: dict[str, Any] = {
        "codec": "alac",
        "frame_count": None,
        "time_base": Decimal(1) / Decimal(48000),
        "stream_duration": Decimal("0.960000"),
    }
    fields.update(overrides)
    return _mp4_header(**fields)


def test_fragmented_mp4_matching_its_span_is_estimated() -> None:
    """A15a: a fragmented MP4 is never complete; lossless is no exception.

    A cut on a fragment boundary shortens the declared duration and the span
    together, so a match proves nothing: the span is measured.
    """
    walk = _walk(*_packets(48))

    assert_that(_decide(walk, header=_fragmented_header())).is_equal_to(
        (Decimal("0.960000"), None, (AudioFlag.ESTIMATED,)),
    )


def test_fragmented_mp4_short_of_its_declared_duration_is_truncated() -> None:
    """A15a: ``truncated=true`` is still allowed, as when a ``sidx`` declares more."""
    walk = _walk(*_packets(36))

    assert_that(_decide(walk, header=_fragmented_header())).is_equal_to(
        (None, True, ()),
    )


def test_fragmented_mp4_past_its_declared_duration_is_estimated() -> None:
    """A15a: whatever the walk finds beyond the declared duration, it is measured."""
    walk = _walk(*_packets(60))

    assert_that(_decide(walk, header=_fragmented_header())).is_equal_to(
        (Decimal("1.200000"), None, (AudioFlag.ESTIMATED,)),
    )


def test_mp4_indexed_in_moov_is_still_checked() -> None:
    """A15a: with every packet indexed in ``moov`` the declared duration is checked."""
    walk = _walk(*_packets(48))

    assert_that(
        _decide(walk, header=_fragmented_header(frame_count=48)),
    ).is_equal_to((Decimal("0.960000"), False, ()))


@pytest.mark.parametrize(
    ("codec", "profile", "expected"),
    [
        ("aac", "HE-AAC", True),
        ("aac", "HE-AACv2", True),
        ("aac", "LC", False),
        ("aac", None, False),
        ("mp3", "HE-AAC", False),
    ],
)
def test_is_he_aac(codec: str, profile: str | None, expected: bool) -> None:
    """A15b: HE-AAC is AAC whose profile ffprobe names as HE-AAC or HE-AACv2."""
    header = _mp4_header(codec=codec, profile=profile)

    assert_that(HE_AAC_PROFILES).is_equal_to(frozenset({"HE-AAC", "HE-AACv2"}))
    assert_that(is_he_aac(header)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("HE-AAC", (Decimal("2.048000"), None, (AudioFlag.ESTIMATED,))),
        ("LC", (Decimal("1.984000"), False, ())),
    ],
)
def test_he_aac_in_mp4_declares_nothing(
    profile: str,
    expected: tuple[Decimal | None, bool | None, tuple[AudioFlag, ...]],
) -> None:
    """A15b: HE-AAC in MP4 declares more than a codec frame past its audio.

    The edit list trims the 64 ms priming from the declared 1.984 s, as in a
    faststart file. The same header with the LC profile is checked as before.
    """
    header = _mp4_header(
        codec="aac",
        profile=profile,
        time_base=Decimal(1) / Decimal(48000),
        stream_duration=Decimal("1.984000"),
    )
    walk = _walk(*_packets(32, step="0.064000", start="-0.064"))

    assert_that(_decide(walk, header=header)).is_equal_to(expected)


def _stretched_walk(first: str) -> PacketWalk:
    """Build a 2 s ALAC walk at 8 kHz whose first packet lasts ``first`` seconds.

    Args:
        first: Duration of the first packet.

    Returns:
        The walk: the first packet, then three 512 ms packets.
    """
    start = Decimal(first)
    rest = [
        f"{start + Decimal('0.512') * index:.6f},0.512000,K__" for index in range(3)
    ]
    return _walk(f"0.000000,{start:.6f},K__", *rest)


@pytest.mark.parametrize(
    ("first", "expected"),
    [("0.552", True), ("0.517", False), ("0.512", False)],
    ids=["40ms", "5ms", "none"],
)
def test_first_packet_stretched(first: str, expected: bool) -> None:
    """A15b: a first packet longer than the median by more than the A5 tolerance.

    40 ms over a 512 ms median is stretched; exactly the 5 ms tolerance is
    not.
    """
    header = _mp4_header(codec="alac", time_base=Decimal(1) / Decimal(8000))

    assert_that(first_packet_stretched(header, _stretched_walk(first))).is_equal_to(
        expected,
    )


def test_first_packet_stretched_needs_a_packet() -> None:
    """With no timed packet there is nothing to compare."""
    assert_that(first_packet_stretched(_mp4_header(), PacketWalk())).is_false()


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        (MP4_FORMAT_NAME, (Decimal("2.088000"), None, (AudioFlag.ESTIMATED,))),
        ("flac", (Decimal("2.088000"), False, ())),
    ],
)
def test_stretched_first_packet_in_mp4_declares_nothing(
    container: str,
    expected: tuple[Decimal | None, bool | None, tuple[AudioFlag, ...]],
) -> None:
    """A15b: in MP4 the stretched span counts the video's delay, so it is measured.

    The rule is for MP4 alone; other containers are checked as before.
    """
    header = _header(
        container=container,
        codec="alac",
        time_base=Decimal(1) / Decimal(8000),
        stream_duration=Decimal("2.088000"),
    )

    assert_that(_decide(_stretched_walk("0.552"), header=header)).is_equal_to(
        expected,
    )


def test_pcm_with_as_many_packets_as_indexed_samples_is_fragmented() -> None:
    """A15g: a cut can leave exactly as many PCM packets as ``nb_frames``.

    Here ``nb_frames`` counts the 2,048 samples ``moov`` indexes, and the walk
    finds 2,048 packets spanning 40.96 s. PCM is compared in samples, so the
    file is fragmented and never reported complete.
    """
    header = _header(
        container=MP4_FORMAT_NAME,
        codec="pcm_s16le",
        frame_count=2048,
    )
    walk = _walk(*_packets(2048))

    assert_that(is_fragmented(header, walk)).is_true()


def test_equal_counts_for_a_packet_codec_are_not_fragmented() -> None:
    """A15g keeps the packet comparison where ``nb_frames`` counts packets."""
    header = _header(container=MP4_FORMAT_NAME, codec="aac", frame_count=2048)
    walk = _walk(*_packets(2048))

    assert_that(is_fragmented(header, walk)).is_false()


@pytest.mark.parametrize("codec", ["adpcm_ms", "adpcm_ima_qt", "adpcm_ima_wav"])
def test_adpcm_in_mp4_has_no_established_duration(codec: str) -> None:
    """A15g: ADPCM packet durations in MP4 do not match the audio.

    A healthy 2 s file whose packets span 1.0 s is never ``truncated``.
    """
    header = _header(
        container=MP4_FORMAT_NAME,
        codec=codec,
        frame_count=None,
        stream_duration=Decimal("2.000000"),
        format_duration=Decimal("2.000000"),
    )
    walk = _walk(*_packets(50))
    outcome = decide_duration(header, walk, run_result(), benign=BENIGN_9)

    assert_that(outcome.duration).is_none()
    assert_that(outcome.truncated).is_none()
    assert_that(outcome.flags).is_equal_to((AudioFlag.INCONSISTENT,))


def test_adpcm_outside_mp4_keeps_its_checks() -> None:
    """A15g is limited to MP4: ADPCM in WAV still reports a cut."""
    header = _header(
        container="wav",
        codec="adpcm_ms",
        stream_duration=Decimal("2.000000"),
        format_duration=Decimal("2.000000"),
    )
    walk = _walk(*_packets(50))
    outcome = decide_duration(header, walk, run_result(), benign=BENIGN_9)

    assert_that(outcome.truncated).is_true()
