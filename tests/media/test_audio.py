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
    known_duration,
    known_flags,
    known_reason,
    probe_json,
    run_result,
)
from winnow.exceptions import MediaError
from winnow.media import audio as audio_module
from winnow.media._ffprobe import DEVNULL, BoundedRun
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
        ["-of", "json=sv=ignore", "-show_format", "-show_streams", "-fd", "0", "fd:"],
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
    assert_that(metadata.truncated).is_false()


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
    assert_that(metadata.truncated).is_false()


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
    """Delayed MKV audio: DURATION tag minus the lead, then checked."""
    metadata = _replay(monkeypatch, tmp_path, "delayed_mkv")

    assert_that(known_duration(metadata)).is_close_to(1.0, 0.05)
    assert_that(metadata.truncated).is_false()
    assert_that(known_flags(metadata)).is_empty()


def test_replay_delayed_mp4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Delayed MP4 audio uses the stream's own duration."""
    metadata = _replay(monkeypatch, tmp_path, "delayed_mp4")

    assert_that(known_duration(metadata)).is_close_to(1.0, 0.05)
    assert_that(metadata.truncated).is_false()


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
    assert_that(metadata.truncated).is_true()
    assert_that(known_flags(metadata)).is_empty()


@pytest.mark.parametrize("name", ["wav_cut_90", "mp3_cut_50", "garbage"])
def test_replay_unlisted_stderr_makes_all_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
) -> None:
    """A header call with an unlisted stderr line or a failure is corrupt."""
    metadata = _replay(monkeypatch, tmp_path, name)

    assert_that(metadata).is_equal_to(MediaMetadata(audio_flags=(AudioFlag.CORRUPT,)))


def test_replay_on_unpinned_major_is_corrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On an unpinned major, the ADTS estimate line is not benign."""
    runner, _banner = _replayer("adts")
    _install(monkeypatch, runner, banner="ffprobe version 10.0 Copyright")
    path = tmp_path / "a.aac"
    path.write_bytes(b"placeholder")

    assert_that(extract_audio_metadata(path).audio_flags).is_equal_to(
        (AudioFlag.CORRUPT,),
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


def test_symlink_form_hides_the_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without the fd protocol ffprobe gets an extension-less private link."""
    seen: list[tuple[list[str], int]] = []
    header = run_result(stdout=json.dumps(probe_json()).encode())
    path = tmp_path / "secret name.flac"
    path.write_bytes(b"fLaC")

    def runner(argv: list[str], *, stdin: int, **kwargs: object) -> BoundedRun:
        seen.append((argv, stdin))
        link = Path(argv[-1].removeprefix("file:"))
        assert_that(link.name).is_equal_to("input")
        assert_that(link.is_absolute()).is_true()
        assert_that(link.resolve()).is_equal_to(path.resolve())
        return header if "-select_streams" not in argv else run_result()

    _install(monkeypatch, runner, fd=False)

    extract_audio_metadata(path)

    assert_that(seen).is_length(2)
    for argv, stdin in seen:
        position = argv.index("-protocol_whitelist")
        assert_that(argv[position + 1]).is_equal_to("file")
        assert_that(argv[-1]).starts_with("file:")
        assert_that(" ".join(argv)).does_not_contain("secret")
        assert_that(stdin).is_equal_to(DEVNULL)
    link = Path(seen[0][0][-1].removeprefix("file:"))
    assert_that(link.parent.exists()).is_false()


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


def test_unopenable_file_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A file that cannot be opened raises MediaError."""

    def refuse(path: object, flags: int) -> int:
        raise PermissionError("denied")

    _install(monkeypatch, _header_then(run_result()))
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
