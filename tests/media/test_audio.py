"""Tests for :func:`extract_audio_metadata` that need no ffprobe.

Recorded ffprobe 9.0.2 output (``fixtures/audio/recorded``) is replayed
through a fake runner, and the runner is replaced to simulate launch failures,
breaches, and the symlink fallback. Neither needs an ffprobe binary, so these
run in CI and carry the coverage gate.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.audio_support import (
    audio_stream,
    known_duration,
    known_flags,
    known_reason,
    probe_json,
    run_result,
)
from winnow.exceptions import MediaError
from winnow.media import audio as audio_module
from winnow.media._audio_rules import HEADER_SHOW_ENTRIES
from winnow.media._ffprobe import BoundedRun, BoundedRunError
from winnow.media.audio import FORMAT_WHITELIST, extract_audio_metadata
from winnow.media.audio import _open_descriptor as open_descriptor
from winnow.models.media import AudioFlag, MediaMetadata

RECORDED_DIR = Path(__file__).resolve().parent / "fixtures" / "audio" / "recorded"
FAKE_BINARY = "/opt/ffmpeg/bin/ffprobe"
BANNER_9 = "ffprobe version 9.0.2 Copyright (c) 2007-2026 the FFmpeg developers"

FakeRunner = Callable[..., BoundedRun]


# --- extract_audio_metadata with a fake runner --------------------------------


def _install(
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    *,
    banner: str | None = BANNER_9,
    fd: bool = True,
) -> None:
    """Replace ffprobe discovery and the runner in the audio module.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        runner: Fake :func:`run_bounded`.
        banner: Version banner to report.
        fd: Whether the fake ffprobe has the fd protocol.
    """
    monkeypatch.setattr(audio_module, "find_binary", lambda name: FAKE_BINARY)
    monkeypatch.setattr(audio_module, "ffprobe_version", lambda binary: banner)
    monkeypatch.setattr(audio_module, "fd_protocol_supported", lambda binary: fd)
    monkeypatch.setattr(audio_module, "run_bounded", runner)


def _replayer(
    name: str,
    seen: list[list[str]] | None = None,
) -> tuple[FakeRunner, str | None]:
    """Build a fake runner that replays a recording.

    Args:
        name: Recording name in ``fixtures/audio/recorded``.
        seen: Receives each argv.

    Returns:
        The runner and the recorded version banner.
    """
    recording = json.loads((RECORDED_DIR / f"{name}.json").read_text())
    calls = iter(recording["calls"])

    def replay(
        argv: list[str],
        *,
        stdin: int,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        on_stdout_line: Callable[[bytes], None] | None = None,
    ) -> BoundedRun:
        call = next(calls)
        expected_kind = "walk" if "-select_streams" in argv else "header"
        assert_that(call["kind"]).is_equal_to(expected_kind)
        if seen is not None:
            seen.append(argv)
        stdout = str(call["stdout"]).encode()
        if on_stdout_line is not None:
            for line in stdout.split(b"\n")[:-1]:
                on_stdout_line(line)
            stdout = b""
        return run_result(
            returncode=int(call["returncode"]),
            stdout=stdout,
            stderr=str(call["stderr"]).encode(),
        )

    return replay, recording["ffprobe_version"]


def _replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    seen: list[list[str]] | None = None,
) -> MediaMetadata:
    """Run the reader against a recording.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Temporary directory.
        name: Recording name.
        seen: Receives each argv.

    Returns:
        The metadata.
    """
    runner, banner = _replayer(name, seen)
    _install(monkeypatch, runner, banner=banner)
    path = tmp_path / "secret name.mp3"
    path.write_bytes(b"placeholder")
    return extract_audio_metadata(path)


def test_recordings_come_from_a_pinned_major() -> None:
    """Every recording was made with an ffprobe whose major is pinned."""
    for path in sorted(RECORDED_DIR.glob("*.json")):
        banner = json.loads(path.read_text())["ffprobe_version"]
        assert_that(banner).described_as(path.name).starts_with(
            "ffprobe version 9.",
        )


def test_replay_declared_mp3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A healthy MP3 with a Xing header: declared duration, bitrate kept."""
    seen: list[list[str]] = []

    metadata = _replay(monkeypatch, tmp_path, "mp3", seen)

    assert_that(metadata).is_equal_to(
        MediaMetadata(
            container="mp3",
            codec="mp3",
            sample_rate=24000,
            channels=1,
            bitrate=8000,
            duration_seconds=6.0,
            audio_stream_count=1,
            probe_score=52,
            truncated=False,
            audio_flags=(),
        ),
    )
    header_argv, walk_argv = seen
    assert_that(header_argv[:8]).is_equal_to(
        [
            FAKE_BINARY,
            "-hide_banner",
            "-v",
            "warning",
            "-protocol_whitelist",
            "fd",
            "-format_whitelist",
            FORMAT_WHITELIST,
        ],
    )
    assert_that(header_argv[8:]).is_equal_to(
        [
            "-of",
            "json=sv=ignore",
            "-show_entries",
            HEADER_SHOW_ENTRIES,
            "-fd",
            "0",
            "fd:",
        ],
    )
    assert_that(walk_argv[8:]).is_equal_to(
        [
            "-select_streams",
            "0",
            "-show_entries",
            "packet=pts_time,duration_time,flags",
            "-of",
            "csv=p=0",
            "-fd",
            "0",
            "fd:",
        ],
    )
    assert_that(header_argv).does_not_contain("-show_format", "-show_streams")
    assert_that(HEADER_SHOW_ENTRIES).does_not_contain("tags")
    for argv in seen:
        assert_that(argv).does_not_contain("-f")
        assert_that(" ".join(argv)).does_not_contain("secret")


def test_replay_adts_is_estimated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADTS AAC: measured duration flagged estimated, bitrate unknown (R7)."""
    metadata = _replay(monkeypatch, tmp_path, "adts")

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.bitrate).is_none()
    assert_that(known_duration(metadata)).is_close_to(2.0, 0.05)


def test_replay_alac_bit_depth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ALAC in MP4 reports codec alac and 16-bit depth."""
    metadata = _replay(monkeypatch, tmp_path, "alac")

    assert_that(metadata.codec).is_equal_to("alac")
    assert_that(metadata.bit_depth).is_equal_to(16)
    assert_that(metadata.truncated).is_same_as(False)
    assert_that(known_flags(metadata)).is_empty()


def test_replay_chained_ogg_is_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Chained Ogg restarts its times: duration unknown, inconsistent."""
    metadata = _replay(monkeypatch, tmp_path, "chained_ogg")

    assert_that(metadata.codec).is_equal_to("opus")
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.INCONSISTENT,))


def test_replay_cover_art(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cover art leaves one audio stream and a declared duration."""
    metadata = _replay(monkeypatch, tmp_path, "cover_mp3")

    assert_that(metadata.audio_stream_count).is_equal_to(1)
    assert_that(metadata.duration_seconds).is_equal_to(6.0)
    assert_that(metadata.truncated).is_same_as(False)
    assert_that(known_flags(metadata)).is_empty()


def test_replay_two_tracks_uses_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The default second track is primary and walked by explicit index."""
    seen: list[list[str]] = []

    metadata = _replay(monkeypatch, tmp_path, "two_tracks", seen)

    assert_that(metadata.sample_rate).is_equal_to(16000)
    assert_that(metadata.channels).is_equal_to(2)
    assert_that(metadata.audio_stream_count).is_equal_to(2)
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    walk_argv = seen[1]
    position = walk_argv.index("-select_streams")
    assert_that(walk_argv[position + 1]).is_equal_to("1")


def test_replay_delayed_matroska(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Delayed MKV audio has no declared duration and is measured (A5)."""
    metadata = _replay(monkeypatch, tmp_path, "delayed_mkv")

    assert_that(known_duration(metadata)).is_close_to(1.0, 0.05)
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))


@pytest.mark.parametrize("name", ["delayed_avi", "video_wmv"])
def test_replay_avi_and_asf_with_video_are_estimated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
) -> None:
    """A9b and A8: AVI never declares, WMV with video does not; both measured."""
    metadata = _replay(monkeypatch, tmp_path, name)

    assert_that(known_duration(metadata)).is_close_to(1.0, 0.07)
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))


def test_replay_delayed_mp4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A9: delayed MP4 audio starts late, so its duration is estimated."""
    metadata = _replay(monkeypatch, tmp_path, "delayed_mp4")

    assert_that(known_duration(metadata)).is_close_to(1.0, 0.05)
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()


def test_replay_truncated_flac(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A FLAC cut to 98 %: facts kept, duration unknown, truncated."""
    metadata = _replay(monkeypatch, tmp_path, "flac_cut_98")

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.bit_depth).is_equal_to(16)
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.bitrate).is_none()
    assert_that(metadata.truncated).is_same_as(True)
    assert_that(known_flags(metadata)).is_empty()


@pytest.mark.parametrize(
    ("name", "flag"),
    [
        ("wav_cut_90", AudioFlag.UNRECOGNIZED_OUTPUT),
        ("mp3_cut_50", AudioFlag.UNRECOGNIZED_OUTPUT),
        ("garbage", AudioFlag.UNSUPPORTED),
    ],
)
def test_replay_unlisted_stderr_makes_all_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    flag: AudioFlag,
) -> None:
    """An unlisted header stderr line is unrecognized; a failed call unsupported.

    A header call that no accepted format can open is not a damage claim
    (A12b).
    """
    metadata = _replay(monkeypatch, tmp_path, name)

    assert_that(metadata).is_equal_to(MediaMetadata(audio_flags=(flag,)))


def test_replay_on_unpinned_major_is_unrecognized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On an unpinned major, the ADTS estimate line is not benign (A1)."""
    runner, _banner = _replayer("adts")
    _install(monkeypatch, runner, banner="ffprobe version 10.0 Copyright")
    path = tmp_path / "a.aac"
    path.write_bytes(b"placeholder")

    assert_that(extract_audio_metadata(path).audio_flags).is_equal_to(
        (AudioFlag.UNRECOGNIZED_OUTPUT,),
    )


def test_replay_cover_art_in_m4a(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A3: M4A with covr art and its timescale line reads normally."""
    metadata = _replay(monkeypatch, tmp_path, "cover_m4a")

    assert_that(
        (
            metadata.container,
            metadata.codec,
            metadata.sample_rate,
            metadata.channels,
            metadata.duration_seconds,
            metadata.audio_stream_count,
            metadata.truncated,
            metadata.audio_flags,
        ),
    ).is_equal_to(
        ("mov,mp4,m4a,3gp,3g2,mj2", "aac", 48000, 1, 2.0, 1, False, ()),
    )
    assert_that(metadata.bitrate).is_not_none()


def test_replay_timecode_track(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A2: a MOV with a tmcd data track reads normally."""
    metadata = _replay(monkeypatch, tmp_path, "timecode_mov")

    assert_that((metadata.codec, metadata.sample_rate)).is_equal_to(("aac", 48000))
    assert_that(metadata.duration_seconds).is_equal_to(2.0)
    assert_that(metadata.truncated).is_same_as(False)
    assert_that(known_flags(metadata)).is_empty()


def test_replay_cut_timecode_track(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A2: the data-track line does not hide a damaged walk."""
    metadata = _replay(monkeypatch, tmp_path, "timecode_mov_cut_90")

    assert_that(metadata.codec).is_equal_to("aac")
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.UNRECOGNIZED_OUTPUT,))


def test_replay_offset_matroska_is_estimated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A9: audio-only Matroska from 5 s is measured, never truncated."""
    metadata = _replay(monkeypatch, tmp_path, "offset_mka")

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(known_duration(metadata)).is_close_to(2.0, 0.05)


@pytest.mark.parametrize(
    ("name", "codec"),
    [("delay_moov", "aac"), ("wma_mka", "wmav2"), ("mp3_frag", "mp3")],
)
def test_replay_priming_counted_as_audio_is_estimated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    codec: str,
) -> None:
    """A13a: a declared duration that counts the priming is not declared.

    ``delay_moov`` (fragmented AAC with ``+delay_moov``) and ``wma_mka``
    declare the packet span of audio that starts before zero; ``mp3_frag``
    is MP3 in fragmented MP4 from zero. All three are 2 s of audio and are
    measured, never checked.
    """
    metadata = _replay(monkeypatch, tmp_path, name)

    assert_that(metadata.codec).is_equal_to(codec)
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.ESTIMATED,))
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.bitrate).is_none()
    assert_that(known_duration(metadata)).is_close_to(2.0, 0.05)


def test_replay_empty_m4a_says_why(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A13c: an M4A with no stream is all unknown and flagged ``inconsistent``."""
    metadata = _replay(monkeypatch, tmp_path, "empty_m4a")

    assert_that(metadata).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,)),
    )


def test_missing_file_raises() -> None:
    """A path that is not a file raises MediaError."""
    with pytest.raises(MediaError):
        extract_audio_metadata(Path("/nonexistent/song.mp3"))


def test_ffprobe_missing_is_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without ffprobe every fact is unknown and the reason is given (R10)."""
    path = tmp_path / "song.mp3"
    path.write_bytes(b"ID3")
    monkeypatch.setattr(audio_module, "find_binary", lambda name: None)

    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(
            audio_flags=(AudioFlag.BACKEND_UNAVAILABLE,),
            audio_backend_reason="ffprobe not found on PATH",
        ),
    )


def test_launch_failure_is_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ffprobe that cannot start is reported as unavailable."""

    def refuse(argv: list[str], **kwargs: object) -> BoundedRun:
        raise PermissionError("not executable")

    _install(monkeypatch, refuse)
    path = tmp_path / "song.mp3"
    path.write_bytes(b"ID3")

    metadata = extract_audio_metadata(path)

    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.BACKEND_UNAVAILABLE,))
    assert_that(known_reason(metadata)).contains("not executable")


def test_header_breach_is_timeout_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A header breach makes every fact unknown with flag timeout (R8)."""
    calls: list[list[str]] = []

    def breach(argv: list[str], **kwargs: object) -> BoundedRun:
        calls.append(argv)
        return run_result(returncode=-9, overflowed=True)

    _install(monkeypatch, breach)
    path = tmp_path / "song.mp3"
    path.write_bytes(b"ID3")

    metadata = extract_audio_metadata(path)

    assert_that(metadata).is_equal_to(MediaMetadata(audio_flags=(AudioFlag.TIMEOUT,)))
    assert_that(calls).is_length(1)


def _header_then(walk: BoundedRun | Exception) -> FakeRunner:
    """Build a runner whose header call succeeds and walk call ends as given.

    Args:
        walk: The walk result, or an exception to raise.

    Returns:
        The fake runner.
    """
    header = run_result(stdout=json.dumps(probe_json()).encode())

    def runner(argv: list[str], **kwargs: object) -> BoundedRun:
        if "-select_streams" not in argv:
            return header
        if isinstance(walk, Exception):
            raise walk
        return walk

    return runner


def test_walk_breach_keeps_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A walk breach makes only duration and damage flags unknown (R8)."""
    _install(monkeypatch, _header_then(run_result(returncode=-9, timed_out=True)))
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.sample_rate).is_equal_to(44100)
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.bitrate).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.TIMEOUT,))


def test_walk_launch_failure_keeps_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A walk that cannot start leaves the header facts in place."""
    _install(monkeypatch, _header_then(OSError("gone")))
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.BACKEND_UNAVAILABLE,))
    assert_that(known_reason(metadata)).contains("gone")


def test_each_call_starts_at_byte_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared descriptor is rewound before each call."""
    offsets: list[int] = []
    header = run_result(stdout=json.dumps(probe_json()).encode())

    def runner(argv: list[str], *, stdin: int, **kwargs: object) -> BoundedRun:
        offsets.append(os.lseek(stdin, 0, os.SEEK_CUR))
        os.lseek(stdin, 100, os.SEEK_SET)
        return header if "-select_streams" not in argv else run_result()

    _install(monkeypatch, runner)
    path = tmp_path / "song.flac"
    path.write_bytes(b"\0" * 1000)

    extract_audio_metadata(path)

    assert_that(offsets).is_equal_to([0, 0])


def _link_target(argv: list[str]) -> str:
    """Return where the private link in a symlink-form argv points.

    Args:
        argv: The ffprobe argument vector.

    Returns:
        The link's target as stored.
    """
    link = Path(argv[-1].removeprefix("file:"))
    assert_that(link.name).is_equal_to("input")
    assert_that(link.is_absolute()).is_true()
    return os.readlink(link)


def test_symlink_form_hides_the_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without the fd protocol ffprobe gets a private link to its own stdin."""
    seen: list[tuple[list[str], int, str]] = []
    header = run_result(stdout=json.dumps(probe_json()).encode())
    path = tmp_path / "secret name.flac"
    path.write_bytes(b"fLaC")

    def runner(argv: list[str], *, stdin: int, **kwargs: object) -> BoundedRun:
        seen.append((argv, stdin, _link_target(argv)))
        assert_that(os.fstat(stdin).st_ino).is_equal_to(path.stat().st_ino)
        return header if "-select_streams" not in argv else run_result()

    _install(monkeypatch, runner, fd=False)

    extract_audio_metadata(path)

    assert_that(seen).is_length(2)
    for argv, _stdin, target in seen:
        position = argv.index("-protocol_whitelist")
        assert_that(argv[position + 1]).is_equal_to("file")
        assert_that(argv[-1]).starts_with("file:")
        assert_that(" ".join(argv)).does_not_contain("secret")
        assert_that(target).is_equal_to("/dev/fd/0")
    link = Path(seen[0][0][-1].removeprefix("file:"))
    assert_that(link.parent.exists()).is_false()


def test_symlink_form_without_dev_fd_links_the_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Where /dev/fd does not exist the link points at the absolute path."""
    targets: list[str] = []
    header = run_result(stdout=json.dumps(probe_json()).encode())
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    def runner(argv: list[str], **kwargs: object) -> BoundedRun:
        targets.append(_link_target(argv))
        return header if "-select_streams" not in argv else run_result()

    _install(monkeypatch, runner, fd=False)
    monkeypatch.setattr(audio_module, "_DESCRIPTOR_DIR", tmp_path / "no-dev-fd")

    metadata = extract_audio_metadata(path)

    assert_that(targets).is_equal_to([str(path.absolute())] * 2)
    assert_that(metadata.codec).is_equal_to("flac")


# --- file changed during the read (A6) ----------------------------------------


def _changing_runner(change: Callable[[], object]) -> FakeRunner:
    """Build a runner that changes the file right after the header call.

    Args:
        change: Changes the file.

    Returns:
        The fake runner.
    """
    header = run_result(stdout=json.dumps(probe_json()).encode())
    walk = run_result(
        stdout=b"".join(
            f"{index * 0.1:.6f},0.100000,K__\n".encode() for index in range(100)
        ),
    )

    def runner(argv: list[str], **kwargs: object) -> BoundedRun:
        if "-select_streams" not in argv:
            change()
            return header
        on_line = kwargs.get("on_stdout_line")
        if callable(on_line):
            for line in walk.stdout.splitlines():
                on_line(line)
        return run_result()

    return runner


@pytest.mark.parametrize("fd", [True, False], ids=["descriptor", "link"])
def test_file_rewritten_in_place_is_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fd: bool,
) -> None:
    """A6: a file rewritten in place between the calls gives all unknown."""
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC" + b"\0" * 100)

    def rewrite() -> None:
        with path.open("ab") as handle:
            handle.write(b"\0" * 100)

    _install(monkeypatch, _changing_runner(rewrite), fd=fd)

    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,)),
    )


def test_file_touched_is_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A6: a changed modification time alone also counts."""
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    def touch() -> None:
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

    _install(monkeypatch, _changing_runner(touch))

    assert_that(extract_audio_metadata(path).audio_flags).is_equal_to(
        (AudioFlag.INCONSISTENT,),
    )


def test_unchanged_file_keeps_its_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A6 does not fire when nothing changed."""
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")
    _install(monkeypatch, _changing_runner(lambda: None))

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.duration_seconds).is_equal_to(10.0)
    assert_that(metadata.truncated).is_same_as(False)
    assert_that(known_flags(metadata)).is_empty()


def test_replaced_path_keeps_the_opened_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Renaming another file over the path does not change what is read.

    Both input forms read the descriptor opened at the start, so the facts
    belong to that one file and are kept.
    """
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")
    other = tmp_path / "other.flac"
    other.write_bytes(b"fLaC" + b"\0" * 500)
    _install(monkeypatch, _changing_runner(lambda: other.replace(path)), fd=False)

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.audio_flags).is_equal_to(())


def test_replaced_path_without_dev_fd_is_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A6: with a link to the path, replacing the path gives all unknown."""
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")
    other = tmp_path / "other.flac"
    other.write_bytes(b"fLaC")
    _install(monkeypatch, _changing_runner(lambda: other.replace(path)), fd=False)
    monkeypatch.setattr(audio_module, "_DESCRIPTOR_DIR", tmp_path / "no-dev-fd")

    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,)),
    )


def test_removed_path_without_dev_fd_is_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A6: with a link to the path, deleting the path gives all unknown."""
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")
    _install(monkeypatch, _changing_runner(path.unlink), fd=False)
    monkeypatch.setattr(audio_module, "_DESCRIPTOR_DIR", tmp_path / "no-dev-fd")

    assert_that(extract_audio_metadata(path).audio_flags).is_equal_to(
        (AudioFlag.INCONSISTENT,),
    )


def test_unreadable_runner_output_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed pipe reader or line callback is an error, never a result."""

    def runner(argv: list[str], **kwargs: object) -> BoundedRun:
        raise BoundedRunError("ffprobe output could not be read")

    _install(monkeypatch, runner)
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    with pytest.raises(MediaError):
        extract_audio_metadata(path)


def test_symlink_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A link that cannot be made raises MediaError rather than guessing."""

    def no_links(self: Path, target: Path) -> None:
        raise OSError("links not allowed")

    _install(monkeypatch, _header_then(run_result()), fd=False)
    monkeypatch.setattr(Path, "symlink_to", no_links)
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    with pytest.raises(MediaError):
        extract_audio_metadata(path)


@pytest.mark.parametrize("fd", [True, False], ids=["descriptor", "link"])
def test_unopenable_file_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fd: bool,
) -> None:
    """A file that cannot be opened raises MediaError in both input forms."""

    def refuse(path: object, flags: int) -> int:
        raise PermissionError("denied")

    _install(monkeypatch, _header_then(run_result()), fd=fd)
    monkeypatch.setattr("winnow.media.audio.os.open", refuse)
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    with pytest.raises(MediaError):
        extract_audio_metadata(path)


def test_descriptor_must_be_a_regular_file(tmp_path: Path) -> None:
    """A FIFO opened without blocking is refused after fstat."""
    fifo = tmp_path / "pipe.mp3"
    os.mkfifo(fifo)

    with pytest.raises(MediaError), open_descriptor(fifo):
        pytest.fail("a FIFO must not be handed to ffprobe")


# --- ffprobe failing is not a statement about the file (A12c) ------------------


@pytest.mark.parametrize("returncode", [-9, -11, 126, 127])
def test_header_program_failure_is_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
) -> None:
    """A12c: a signal or exit 126/127 on the header call is not about the file."""
    _install(monkeypatch, lambda argv, **kwargs: run_result(returncode=returncode))
    path = tmp_path / "song.mp3"
    path.write_bytes(b"ID3")

    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.BACKEND_UNAVAILABLE,)),
    )


def test_header_other_exit_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A12b: any other non-zero header exit means no accepted format matched."""
    _install(monkeypatch, lambda argv, **kwargs: run_result(returncode=1))
    path = tmp_path / "song.au"
    path.write_bytes(b".snd")

    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.UNSUPPORTED,)),
    )


@pytest.mark.parametrize("returncode", [-9, 126, 127])
def test_walk_program_failure_keeps_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
) -> None:
    """A12c: a walk killed by a signal or not run is not corrupt."""
    _install(monkeypatch, _header_then(run_result(returncode=returncode)))
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.BACKEND_UNAVAILABLE,))


@pytest.mark.parametrize(
    "banner",
    [None, "", "ffprobe version N-121212-gdeadbeef Copyright", "garbage"],
    ids=["unreadable", "empty", "git_build", "garbage"],
)
def test_unparsed_version_probes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    banner: str | None,
) -> None:
    """A12c: without a readable version nothing is probed."""
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> BoundedRun:
        calls.append(argv)
        return run_result()

    _install(monkeypatch, runner, banner=banner)
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    metadata = extract_audio_metadata(path)

    assert_that(calls).is_empty()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.BACKEND_UNAVAILABLE,))
    assert_that(known_reason(metadata)).contains("-version")


def test_debian_epoch_banner_uses_the_major_pins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A12c: ``7:9.0.2-1`` is major 9, so the 9.x benign lines apply."""
    runner, _banner = _replayer("adts")
    _install(monkeypatch, runner, banner="ffprobe version 7:9.0.2-1 Copyright")
    path = tmp_path / "a.aac"
    path.write_bytes(b"placeholder")

    assert_that(extract_audio_metadata(path).audio_flags).is_equal_to(
        (AudioFlag.ESTIMATED,),
    )


# --- out-of-range values (A12f) ------------------------------------------------

_HUGE = "9" * 18
_HUGE_DECIMAL = "999999999999999.999999999999"


def _walk_lines(count: int = 100, step: str = "0.100000") -> bytes:
    """Build healthy walk output.

    Args:
        count: Packets.
        step: Packet duration.

    Returns:
        CSV stdout.
    """
    return b"".join(
        f"{index * float(step):.6f},{step},K__\n".encode() for index in range(count)
    )


def _json_runner(data: dict[str, object], walk: bytes) -> FakeRunner:
    """Build a runner returning ``data`` for the header and ``walk`` lines.

    Args:
        data: Header JSON.
        walk: Walk stdout.

    Returns:
        The fake runner.
    """

    def runner(argv: list[str], **kwargs: object) -> BoundedRun:
        if "-select_streams" not in argv:
            return run_result(stdout=json.dumps(data).encode())
        on_line = kwargs.get("on_stdout_line")
        if callable(on_line):
            for line in walk.splitlines():
                on_line(line)
        return run_result()

    return runner


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("format", "probe_score", 101),
        ("format", "probe_score", -1),
        ("format", "probe_score", 2**63),
        ("format", "probe_score", "50"),
        ("format", "duration", "-10.000000"),
        ("format", "duration", _HUGE_DECIMAL),
        ("format", "start_time", "-" + _HUGE_DECIMAL),
        ("format", "start_time", _HUGE_DECIMAL),
        ("stream", "sample_rate", "-44100"),
        ("stream", "sample_rate", _HUGE),
        ("stream", "sample_rate", 2**80),
        ("stream", "channels", -2),
        ("stream", "channels", 2**80),
        ("stream", "bits_per_raw_sample", "-16"),
        ("stream", "bits_per_raw_sample", _HUGE),
        ("stream", "bits_per_sample", -16),
        ("stream", "bits_per_sample", 2**80),
        ("stream", "bit_rate", "-900000"),
        ("stream", "bit_rate", _HUGE),
        ("stream", "duration", "-10.000000"),
        ("stream", "duration", "0.000000"),
        ("stream", "duration", _HUGE_DECIMAL),
        ("stream", "time_base", "1/9999999999"),
        ("stream", "time_base", "9999999999/1"),
        ("stream", "index", -1),
        ("stream", "index", 2**80),
    ],
)
def test_out_of_range_header_values_never_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    section: str,
    key: str,
    value: object,
) -> None:
    """A12f: no numeric value from ffprobe raises a ValidationError."""
    stream = audio_stream()
    data = probe_json(stream)
    target = stream if section == "stream" else data["format"]
    if not isinstance(target, dict):
        pytest.fail("the format section is not an object")
    target[key] = value
    _install(monkeypatch, _json_runner(data, _walk_lines()))
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    metadata = extract_audio_metadata(path)

    if metadata.probe_score is not None:
        assert_that(metadata.probe_score).is_between(0, 100)
    if metadata.duration_seconds is not None:
        assert_that(metadata.duration_seconds).is_positive()


@pytest.mark.parametrize(
    "walk",
    [
        b"0.000000,-0.100000,K__\n" * 3,
        b"0.000000,0.000000,K__\n0.000000,0.000000,K__\n",
        f"{_HUGE_DECIMAL},{_HUGE_DECIMAL},K__\n".encode(),
        f"-{_HUGE_DECIMAL},0.100000,K__\n0.0,0.1,K__\n".encode(),
    ],
    ids=["negative", "zero", "huge", "huge_negative_start"],
)
def test_out_of_range_walk_values_never_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    walk: bytes,
) -> None:
    """A12e and A12f: the walk never gives a zero, negative, or invalid value."""
    data = probe_json(audio_stream(duration=None), format_name="ogg")
    _install(monkeypatch, _json_runner(data, walk))
    path = tmp_path / "song.ogg"
    path.write_bytes(b"OggS")

    metadata = extract_audio_metadata(path)

    if metadata.duration_seconds is not None:
        assert_that(metadata.duration_seconds).is_positive()


def test_probe_score_out_of_range_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A12f: a probe score above 100 is unknown; the other facts stay."""
    data = probe_json(probe_score=101)
    _install(monkeypatch, _json_runner(data, _walk_lines()))
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    metadata = extract_audio_metadata(path)

    assert_that(metadata.probe_score).is_none()
    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.truncated).is_same_as(False)


# --- the path fallback compares path and descriptor (A12g) --------------------


def test_path_not_naming_the_descriptor_probes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A12g: a path that names another file before the probe is inconsistent."""
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> BoundedRun:
        calls.append(argv)
        return run_result()

    _install(monkeypatch, runner, fd=False)
    monkeypatch.setattr(audio_module, "_DESCRIPTOR_DIR", tmp_path / "no-dev-fd")
    monkeypatch.setattr(audio_module, "_path_identity", lambda path: (-1,))
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")

    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,)),
    )
    assert_that(calls).is_empty()


def test_path_replaced_by_identical_twin_is_inconsistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A12g: a same-size, same-mtime file over the path is still another file.

    Comparing the path before and after alone would miss it; comparing the
    path with the opened descriptor does not.
    """
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")
    twin = tmp_path / "twin.flac"
    twin.write_bytes(b"fLaC")
    info = path.stat()
    os.utime(twin, ns=(info.st_atime_ns, info.st_mtime_ns))

    def swap() -> None:
        twin.replace(path)

    _install(monkeypatch, _changing_runner(swap), fd=False)
    monkeypatch.setattr(audio_module, "_DESCRIPTOR_DIR", tmp_path / "no-dev-fd")

    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.INCONSISTENT,)),
    )


def test_path_fallback_with_matching_path_keeps_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A12g: when the path names the opened file, the facts stand."""
    path = tmp_path / "song.flac"
    path.write_bytes(b"fLaC")
    _install(monkeypatch, _changing_runner(lambda: None), fd=False)
    monkeypatch.setattr(audio_module, "_DESCRIPTOR_DIR", tmp_path / "no-dev-fd")

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.truncated).is_same_as(False)
    assert_that(known_flags(metadata)).is_empty()
