"""Unit tests for the pure audio rules (issue #400), no ffprobe needed.

Hand-built ffprobe output is fed to the functions in
:mod:`winnow.media._audio_rules`: stderr filtering (R2), strict JSON (R1),
stream selection and facts (R2 to R4), the declared duration (R5), and the
packet-walk decision (R6).
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
        stream_duration=Decimal("2.000000"),
        tag_duration=None,
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
    """The 9.x pins are exactly the four lines named in R2."""
    assert_that(BENIGN_9).is_equal_to(
        frozenset(
            {
                "Estimating duration from bitrate, this may be inaccurate",
                "invalid concatenated file detected - using bitrate for duration",
                "ignoring wrong sample_count",
                "Ignoring maximum wav data size",
            },
        ),
    )
    assert_that(ESTIMATE_STDERR_LINES <= BENIGN_9).is_true()


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


def test_header_from_json_parses_matroska_duration_tag() -> None:
    """A Matroska DURATION tag is parsed to seconds."""
    data = probe_json(
        audio_stream(duration=None, tags={"DURATION": "01:02:03.500000000"}),
        format_name="matroska,webm",
    )

    header = _accepted(data)

    assert_that(header.stream_duration).is_none()
    assert_that(header.tag_duration).is_equal_to(Decimal("3723.500000000"))


@pytest.mark.parametrize(
    "tags",
    ["nope", {}, {"DURATION": 5}, {"DURATION": "2s"}, {"DURATION": "0:00:00"}],
)
def test_header_ignores_bad_duration_tag(tags: object) -> None:
    """Missing, malformed, or zero DURATION tags are unknown."""
    data = probe_json(audio_stream(tags=tags), format_name="matroska,webm")

    assert_that(_accepted(data).tag_duration).is_none()


def test_interpret_header_flags_nonzero_exit() -> None:
    """A failed header call is corrupt even with clean stderr."""
    run = run_result(returncode=1, stdout=json.dumps(probe_json()).encode())

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.CORRUPT,)),
    )


def test_interpret_header_flags_unlisted_stderr() -> None:
    """Exit 0 is not trusted: any unlisted stderr line is corrupt."""
    run = run_result(
        stdout=json.dumps(probe_json()).encode(),
        stderr=b"[flac @ 0x1] Packet corrupt (stream = 0, dts = 0).\n",
    )

    assert_that(interpret_header(run, benign=BENIGN_9)).is_equal_to(
        (None, (AudioFlag.CORRUPT,)),
    )


def test_interpret_header_without_pins_refuses_benign_line() -> None:
    """On an unpinned major even a 9.x benign line is corrupt."""
    run = run_result(
        stdout=json.dumps(probe_json()).encode(),
        stderr=f"[mp3 @ 0x1] {ESTIMATE_LINE}\n".encode(),
    )

    assert_that(interpret_header(run, benign=frozenset())).is_equal_to(
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

    assert_that(
        declared_duration(single, _walk(), estimate_warned=False),
    ).is_equal_to(Decimal(7))
    assert_that(declared_duration(several, _walk(), estimate_warned=False)).is_none()


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
        "tag_duration": Decimal("2.008"),
        "format_duration": Decimal("2.008"),
        "non_picture_stream_count": 2,
    }
    fields.update(overrides)
    return _header(**fields)


def test_declared_matroska_tag_minus_lead() -> None:
    """A lead over three packets is subtracted from the DURATION tag."""
    walk = _walk(*_packets(50, start="0.994"))

    assert_that(
        declared_duration(_matroska_header(), walk, estimate_warned=False),
    ).is_equal_to(Decimal("1.014"))


def test_declared_matroska_short_lead_falls_through() -> None:
    """A lead of three packets or less does not use the tag."""
    walk = _walk(*_packets(50, start="0.060"))

    assert_that(
        declared_duration(_matroska_header(), walk, estimate_warned=False),
    ).is_none()
    single = _matroska_header(non_picture_stream_count=1)
    assert_that(
        declared_duration(single, walk, estimate_warned=False),
    ).is_equal_to(Decimal("2.008"))


@pytest.mark.parametrize(
    "overrides",
    [{"format_start": None}, {"tag_duration": None}, {"tag_duration": Decimal(1)}],
    ids=["no_start_time", "no_tag", "tag_shorter_than_lead"],
)
def test_declared_matroska_tag_needs_all_parts(overrides: dict[str, Any]) -> None:
    """Without a start time, a tag, or a positive result the tag is unused."""
    walk = _walk(*_packets(50, start="1.5"))

    assert_that(
        declared_duration(_matroska_header(**overrides), walk, estimate_warned=False),
    ).is_none()


def test_declared_matroska_without_packets() -> None:
    """The tag rule needs a first packet time."""
    assert_that(
        declared_duration(_matroska_header(), PacketWalk(), estimate_warned=False),
    ).is_none()


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
    """Step 1 wins over steps 2 to 4."""
    stderr = b"[flac @ 0x1] File ended prematurely\n"

    assert_that(_decide(_walk(), stderr=stderr)).is_equal_to(
        (None, None, (AudioFlag.CORRUPT,)),
    )


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


def test_step6_short_by_more_than_one_packet_is_truncated() -> None:
    """Step 6: measured more than one median packet short is truncated."""
    assert_that(_decide(_walk(*_packets(98)))).is_equal_to((None, True, ()))


def test_step6_boundary_one_packet_short_is_not_truncated() -> None:
    """Exactly one median packet short is not more than one packet."""
    assert_that(_decide(_walk(*_packets(99)))).is_equal_to(
        (Decimal("2.000000"), False, ()),
    )


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
