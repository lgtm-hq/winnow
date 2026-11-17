"""Tests for the shared FFmpeg runner and the R8 guards.

The bounded runner is driven with small Python child processes, and the audio
reader with a stub ``ffprobe`` script on ``PATH`` that sleeps or floods, so
nothing here needs a real ffprobe.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that

from winnow.media import _ffprobe
from winnow.media import audio as audio_module
from winnow.media._ffprobe import (
    ADDRESS_SPACE_LIMIT_BYTES,
    DEVNULL,
    fd_protocol_supported,
    ffprobe_available,
    ffprobe_version,
    find_binary,
    limit_address_space,
    parse_version_major,
    probe_environment,
    run_bounded,
)
from winnow.media.audio import extract_audio_metadata
from winnow.models.media import AudioFlag, MediaMetadata

RECORDED_DIR = Path(__file__).resolve().parent / "fixtures" / "audio" / "recorded"
PYTHON = sys.executable


def _python(code: str) -> list[str]:
    """Return argv that runs ``code`` in a child Python.

    Args:
        code: Python source.

    Returns:
        The argument vector.
    """
    return [PYTHON, "-c", code]


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Reset the cached version and protocol answers around each test.

    Yields:
        Control to the test.
    """
    ffprobe_version.cache_clear()
    fd_protocol_supported.cache_clear()
    yield
    ffprobe_version.cache_clear()
    fd_protocol_supported.cache_clear()


# --- environment and discovery ------------------------------------------------


def test_probe_environment_removes_report_and_fixes_locale() -> None:
    """FFREPORT is dropped, colour is off, and the locale is C."""
    env = probe_environment({"FFREPORT": "file=x.log", "LANG": "de_DE", "KEEP": "1"})

    assert_that(env).is_equal_to(
        {"KEEP": "1", "AV_LOG_FORCE_NOCOLOR": "1", "LC_ALL": "C", "LANG": "C"},
    )


def test_probe_environment_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a base mapping the process environment is used."""
    monkeypatch.setenv("FFREPORT", "file=leak.log")
    monkeypatch.setenv("WINNOW_RUNNER_PROBE", "yes")

    env = probe_environment()

    assert_that(env).does_not_contain_key("FFREPORT")
    assert_that(env["WINNOW_RUNNER_PROBE"]).is_equal_to("yes")


def test_find_binary_returns_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative PATH hit is made absolute."""
    monkeypatch.setattr(
        "winnow.media._ffprobe.shutil.which", lambda name: "bin/ffprobe"
    )

    found = find_binary("ffprobe")

    assert_that(found).is_not_none()
    assert_that(Path(str(found)).is_absolute()).is_true()
    assert_that(ffprobe_available()).is_true()


def test_find_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool that is not on PATH resolves to None."""
    monkeypatch.setattr("winnow.media._ffprobe.shutil.which", lambda name: None)

    assert_that(find_binary("ffprobe")).is_none()
    assert_that(ffprobe_available()).is_false()


@pytest.mark.parametrize(
    ("banner", "major"),
    [
        ("ffprobe version 9.0.2 Copyright (c) 2007-2026", 9),
        ("ffprobe version n7.1.1 Copyright", 7),
        ("ffprobe version N-118000-g1234abcd Copyright", None),
        ("ffmpeg version 9.0.2", None),
        ("", None),
    ],
)
def test_parse_version_major(banner: str, major: int | None) -> None:
    """Release banners give a major; git builds and others do not."""
    assert_that(parse_version_major(banner)).is_equal_to(major)


def test_tested_majors_match_pinned_majors() -> None:
    """The doctor's tested range is the set of pinned majors."""
    assert_that(_ffprobe.TESTED_FFPROBE_MAJORS).is_equal_to(
        frozenset(audio_module.BENIGN_STDERR_BY_MAJOR),
    )


# --- RLIMIT_AS ------------------------------------------------------------------


def _fake_resource(hard: int, infinity: int = -1) -> SimpleNamespace:
    """Build a stand-in for the ``resource`` module that records calls.

    Args:
        hard: Hard limit to report.
        infinity: Value of ``RLIM_INFINITY``.

    Returns:
        The fake module; ``calls`` lists each ``setrlimit`` argument.
    """
    calls: list[tuple[int, tuple[int, int]]] = []
    return SimpleNamespace(
        RLIMIT_AS=9,
        RLIM_INFINITY=infinity,
        getrlimit=lambda which: (infinity, hard),
        setrlimit=lambda which, limits: calls.append((which, limits)),
        calls=calls,
    )


def test_limit_address_space_sets_one_gibibyte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no hard limit the soft limit becomes 1 GiB."""
    fake = _fake_resource(hard=-1)
    monkeypatch.setitem(sys.modules, "resource", fake)

    limit_address_space()

    assert_that(fake.calls).is_equal_to([(9, (ADDRESS_SPACE_LIMIT_BYTES, -1))])


def test_limit_address_space_keeps_lower_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard limit below 1 GiB is kept."""
    fake = _fake_resource(hard=4096)
    monkeypatch.setitem(sys.modules, "resource", fake)

    limit_address_space()

    assert_that(fake.calls).is_equal_to([(9, (4096, 4096))])


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("linux", limit_address_space), ("darwin", None), ("win32", None)],
)
def test_preexec_only_on_linux(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: object,
) -> None:
    """The address-space cap is applied on Linux only."""
    monkeypatch.setattr("winnow.media._ffprobe.sys.platform", platform)

    assert_that(_ffprobe._preexec()).is_equal_to(expected)


# --- run_bounded ------------------------------------------------------------


def test_run_bounded_captures_output_and_status() -> None:
    """Stdout, stderr, and the exit status are returned."""
    run = run_bounded(
        _python(
            "import sys; sys.stdout.write('out'); sys.stderr.write('err'); "
            "sys.exit(3)",
        ),
        stdin=DEVNULL,
        timeout=30,
        stdout_limit=1024,
        stderr_limit=1024,
    )

    assert_that((run.returncode, run.stdout, run.stderr)).is_equal_to(
        (3, b"out", b"err"),
    )
    assert_that(run.breached).is_false()


def test_run_bounded_passes_environment() -> None:
    """The child sees the guarded environment."""
    run = run_bounded(
        _python("import os; print(os.environ.get('AV_LOG_FORCE_NOCOLOR'))"),
        stdin=DEVNULL,
        timeout=30,
        stdout_limit=1024,
        stderr_limit=1024,
    )

    assert_that(run.stdout.strip()).is_equal_to(b"1")


def test_run_bounded_streams_lines_to_callback() -> None:
    """Lines arrive one by one, including a last line without a newline."""
    lines: list[bytes] = []

    run = run_bounded(
        _python("import sys; sys.stdout.write('a,1\\nb,2\\n' + 'c' * 70000)"),
        stdin=DEVNULL,
        timeout=30,
        stdout_limit=1_000_000,
        stderr_limit=1024,
        on_stdout_line=lines.append,
    )

    assert_that(lines).is_equal_to([b"a,1", b"b,2", b"c" * 70000])
    assert_that(run.stdout).is_empty()


def test_run_bounded_reads_stdin_descriptor(tmp_path: Path) -> None:
    """The given descriptor is the child's stdin."""
    source = tmp_path / "data.bin"
    source.write_bytes(b"payload")
    descriptor = os.open(source, os.O_RDONLY)
    try:
        run = run_bounded(
            _python("import sys; sys.stdout.write(sys.stdin.read())"),
            stdin=descriptor,
            timeout=30,
            stdout_limit=1024,
            stderr_limit=1024,
        )
    finally:
        os.close(descriptor)

    assert_that(run.stdout).is_equal_to(b"payload")


def test_run_bounded_kills_on_timeout() -> None:
    """A child that outlives the time-out is killed and reaped."""
    started = time.monotonic()

    run = run_bounded(
        _python("import time; time.sleep(60)"),
        stdin=DEVNULL,
        timeout=0.5,
        stdout_limit=1024,
        stderr_limit=1024,
    )

    assert_that(time.monotonic() - started).is_less_than(20)
    assert_that(run.timed_out).is_true()
    assert_that(run.breached).is_true()
    assert_that(run.returncode).is_not_equal_to(0)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_run_bounded_kills_on_flood(stream: str) -> None:
    """A child that writes past a bound is killed."""
    started = time.monotonic()

    run = run_bounded(
        _python(
            f"import sys, time\n"
            f"for _ in range(100000):\n"
            f"    sys.{stream}.write('x' * 4096); sys.{stream}.flush()\n"
            f"time.sleep(60)",
        ),
        stdin=DEVNULL,
        timeout=60,
        stdout_limit=64 * 1024,
        stderr_limit=64 * 1024,
        on_stdout_line=lambda line: None,
    )

    assert_that(time.monotonic() - started).is_less_than(30)
    assert_that(run.overflowed).is_true()
    assert_that(run.timed_out).is_false()
    assert_that(run.returncode).is_not_equal_to(0)


def test_run_bounded_launch_failure(tmp_path: Path) -> None:
    """A binary that does not exist raises OSError."""
    with pytest.raises(OSError):
        run_bounded(
            [str(tmp_path / "missing")],
            stdin=DEVNULL,
            timeout=1,
            stdout_limit=1,
            stderr_limit=1,
        )


def test_pipe_requires_a_stream() -> None:
    """A missing pipe is an OSError."""
    with pytest.raises(OSError):
        _ffprobe._pipe(None)


# --- stub ffprobe -------------------------------------------------------------

_STUB = """#!{python}
import json, os, sys, time
argv = sys.argv[1:]
log = os.environ.get("STUB_ARGV_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps(argv) + "\\n")
mode = os.environ.get("STUB_MODE", "")
if argv == ["-version"]:
    if mode == "version-fails":
        sys.exit(1)
    print(os.environ.get("STUB_BANNER", "ffprobe version 9.0.2 Copyright stub"))
    sys.exit(0)
if argv == ["-hide_banner", "-protocols"]:
    print("Supported file protocols:")
    print("Input:")
    print("  file")
    if mode != "no-fd":
        print("  fd")
    print("Output:")
    print("  fd")
    sys.exit(0)
walk = "-select_streams" in argv
if mode == "sleep" or (mode == "walk-sleep" and walk):
    time.sleep(60)
if mode == "flood" or (mode == "walk-flood" and walk):
    while True:
        sys.stdout.write("0.0,0.02,K__\\n" * 1000)
        sys.stdout.flush()
if mode == "stderr-flood":
    while True:
        sys.stderr.write("noise\\n" * 1000)
        sys.stderr.flush()
recording = json.load(open(os.environ["STUB_RECORDING"]))
call = [c for c in recording["calls"] if c["kind"] == ("walk" if walk else "header")]
sys.stdout.write(call[0]["stdout"])
sys.stderr.write(call[0]["stderr"])
sys.exit(call[0]["returncode"])
"""


@pytest.fixture
def stub_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Put a stub ``ffprobe`` first on PATH, replaying the MP3 recording.

    Args:
        tmp_path: Temporary directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The file that collects the stub's argument vectors.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "ffprobe"
    stub.write_text(_STUB.format(python=PYTHON))
    stub.chmod(0o755)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("STUB_RECORDING", str(RECORDED_DIR / "mp3.json"))
    monkeypatch.setenv("STUB_ARGV_LOG", str(log))
    monkeypatch.setattr(audio_module, "HEADER_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(audio_module, "WALK_BASE_TIMEOUT_SECONDS", 1.0)
    return log


def _audio_file(tmp_path: Path) -> Path:
    """Write a placeholder file with a revealing name.

    Args:
        tmp_path: Temporary directory.

    Returns:
        The file path.
    """
    path = tmp_path / "-revealing name.mp3"
    path.write_bytes(b"\0" * 2048)
    return path


def test_stub_replay_end_to_end(stub_ffprobe: Path, tmp_path: Path) -> None:
    """Through a real process, the stub's recorded output yields the facts."""
    metadata = extract_audio_metadata(_audio_file(tmp_path))

    assert_that(metadata.codec).is_equal_to("mp3")
    assert_that(metadata.duration_seconds).is_equal_to(6.0)
    assert_that(metadata.truncated).is_false()
    logged = [json.loads(line) for line in stub_ffprobe.read_text().splitlines()]
    probe_calls = [argv for argv in logged if argv[-1] == "fd:"]
    assert_that(probe_calls).is_length(2)
    for argv in probe_calls:
        assert_that(argv).does_not_contain("-f")
        assert_that(" ".join(argv)).does_not_contain("revealing")


def test_stub_without_fd_uses_private_link(
    stub_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ffprobe without the fd protocol gets file:<private link>."""
    monkeypatch.setenv("STUB_MODE", "no-fd")

    metadata = extract_audio_metadata(_audio_file(tmp_path))

    assert_that(metadata.codec).is_equal_to("mp3")
    logged = [json.loads(line) for line in stub_ffprobe.read_text().splitlines()]
    probe_calls = [argv for argv in logged if argv[-1].startswith("file:")]
    assert_that(probe_calls).is_length(2)
    for argv in probe_calls:
        assert_that(Path(argv[-1].removeprefix("file:")).name).is_equal_to("input")


def test_stub_that_sleeps_times_out(
    stub_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A header call that hangs is killed: all unknown, flag timeout."""
    monkeypatch.setenv("STUB_MODE", "sleep")
    started = time.monotonic()

    metadata = extract_audio_metadata(_audio_file(tmp_path))

    assert_that(time.monotonic() - started).is_less_than(20)
    assert_that(metadata).is_equal_to(MediaMetadata(audio_flags=(AudioFlag.TIMEOUT,)))


@pytest.mark.parametrize("mode", ["flood", "stderr-flood"])
def test_stub_that_floods_is_killed(
    stub_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """A header call that floods output is killed: all unknown, timeout."""
    monkeypatch.setenv("STUB_MODE", mode)
    monkeypatch.setattr(audio_module, "HEADER_STDOUT_LIMIT", 64 * 1024)
    started = time.monotonic()

    metadata = extract_audio_metadata(_audio_file(tmp_path))

    assert_that(time.monotonic() - started).is_less_than(20)
    assert_that(metadata).is_equal_to(MediaMetadata(audio_flags=(AudioFlag.TIMEOUT,)))


@pytest.mark.parametrize("mode", ["walk-sleep", "walk-flood"])
def test_walk_breach_keeps_header_facts(
    stub_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """A walk that hangs or floods loses only duration and damage flags."""
    monkeypatch.setenv("STUB_MODE", mode)
    monkeypatch.setattr(audio_module, "WALK_STDOUT_LIMIT", 64 * 1024)
    started = time.monotonic()

    metadata = extract_audio_metadata(_audio_file(tmp_path))

    assert_that(time.monotonic() - started).is_less_than(20)
    assert_that(metadata.codec).is_equal_to("mp3")
    assert_that(metadata.sample_rate).is_equal_to(24000)
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.bitrate).is_none()
    assert_that(metadata.truncated).is_none()
    assert_that(metadata.audio_flags).is_equal_to((AudioFlag.TIMEOUT,))


def test_stub_version_and_protocols(stub_ffprobe: Path) -> None:
    """The version banner and fd protocol are read from the binary."""
    binary = find_binary("ffprobe")

    assert_that(binary).is_not_none()
    banner = ffprobe_version(str(binary))
    if banner is None:
        pytest.fail("the stub banner was not read")
    assert_that(banner).starts_with("ffprobe version 9.0.2")
    assert_that(fd_protocol_supported(str(binary))).is_true()


def test_stub_version_failure(
    stub_ffprobe: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing -version run gives no banner and so no benign lines."""
    monkeypatch.setenv("STUB_MODE", "version-fails")
    binary = str(find_binary("ffprobe"))

    assert_that(ffprobe_version(binary)).is_none()
    assert_that(audio_module.benign_stderr_lines(binary)).is_empty()


def test_info_queries_on_missing_binary(tmp_path: Path) -> None:
    """A binary that cannot start has no version and no fd protocol."""
    missing = str(tmp_path / "nope")

    assert_that(ffprobe_version(missing)).is_none()
    assert_that(fd_protocol_supported(missing)).is_false()


def test_stub_on_unpinned_major_rejects_estimates(
    stub_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an unpinned major the ADTS estimate line makes the file corrupt."""
    monkeypatch.setenv("STUB_BANNER", "ffprobe version 10.1 Copyright stub")
    monkeypatch.setenv("STUB_RECORDING", str(RECORDED_DIR / "adts.json"))

    metadata = extract_audio_metadata(_audio_file(tmp_path))

    assert_that(metadata).is_equal_to(MediaMetadata(audio_flags=(AudioFlag.CORRUPT,)))
