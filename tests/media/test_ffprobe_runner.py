"""Tests for the shared FFmpeg runner and the R8 guards.

The bounded runner is driven with small Python child processes, and the audio
reader with a stub ``ffprobe`` script on ``PATH`` that sleeps or floods, so
nothing here needs a real ffprobe.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that

from winnow.exceptions import MediaError
from winnow.media import _ffprobe
from winnow.media import audio as audio_module
from winnow.media._ffprobe import (
    ADDRESS_SPACE_LIMIT_BYTES,
    DEVNULL,
    BoundedRunError,
    fd_protocol_supported,
    ffprobe_available,
    ffprobe_version,
    find_binary,
    limit_address_space,
    parse_version_major,
    probe_environment,
    run_bounded,
    run_command,
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


def _recording_spawn(
    spawned: list[object],
    options: list[dict[str, object]],
) -> Callable[..., object]:
    """Wrap the real launcher so each started process is kept.

    Args:
        spawned: Receives each started process.
        options: Receives the keyword options of each start.

    Returns:
        A drop-in replacement for ``_spawn``.
    """
    real_spawn = _ffprobe._spawn

    def spawn(
        argv: list[str],
        *,
        stdin: int | None,
        text: bool,
        env: Mapping[str, str] | None = None,
        isolate: bool = False,
    ) -> object:
        options.append({"stdin": stdin, "text": text, "isolate": isolate})
        process = real_spawn(argv, stdin=stdin, text=text, env=env, isolate=isolate)
        spawned.append(process)
        return process

    return spawn


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
        The fake module; ``calls`` lists each ``prlimit`` argument tuple.
    """
    calls: list[tuple[object, ...]] = []

    def prlimit(*args: object) -> tuple[int, int]:
        calls.append(args)
        return (infinity, hard)

    return SimpleNamespace(
        RLIMIT_AS=9,
        RLIM_INFINITY=infinity,
        prlimit=prlimit,
        calls=calls,
    )


def test_limit_address_space_sets_one_gibibyte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Linux, with no hard limit, the child's soft limit becomes 1 GiB."""
    fake = _fake_resource(hard=-1)
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setattr("winnow.media._ffprobe.sys.platform", "linux")

    limit_address_space(4242)

    assert_that(fake.calls).is_equal_to(
        [(4242, 9), (4242, 9, (ADDRESS_SPACE_LIMIT_BYTES, -1))],
    )


def test_limit_address_space_keeps_lower_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard limit below 1 GiB is kept."""
    fake = _fake_resource(hard=4096)
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setattr("winnow.media._ffprobe.sys.platform", "linux")

    limit_address_space(7)

    assert_that(fake.calls[-1]).is_equal_to((7, 9, (4096, 4096)))


def test_limit_address_space_ignores_an_exited_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that is already gone needs no limit."""

    def gone(*args: object) -> tuple[int, int]:
        raise ProcessLookupError(args)

    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(RLIMIT_AS=9, RLIM_INFINITY=-1, prlimit=gone),
    )
    monkeypatch.setattr("winnow.media._ffprobe.sys.platform", "linux")

    limit_address_space(7)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_limit_address_space_only_on_linux(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    """Off Linux nothing is limited and ``resource`` is not used."""
    fake = _fake_resource(hard=-1)
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setattr("winnow.media._ffprobe.sys.platform", platform)

    limit_address_space(7)

    assert_that(fake.calls).is_empty()


def test_run_bounded_limits_the_started_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limit is applied to the child's pid from the parent, no preexec."""
    limited: list[int] = []
    spawned: list[object] = []
    options: list[dict[str, object]] = []
    monkeypatch.setattr(_ffprobe, "limit_address_space", limited.append)
    monkeypatch.setattr(_ffprobe, "_spawn", _recording_spawn(spawned, options))

    run = run_bounded(
        _python("print(1)"),
        stdin=DEVNULL,
        timeout=30,
        stdout_limit=1024,
        stderr_limit=1024,
    )

    assert_that(run.returncode).is_equal_to(0)
    assert_that(limited).is_equal_to([getattr(spawned[0], "pid", None)])
    assert_that(options[0]["isolate"]).is_true()


def test_run_bounded_kills_the_child_when_the_limit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If the limit cannot be set, the child is killed and the error raised."""
    marker = tmp_path / "pid"

    def refuse(pid: int) -> None:
        marker.write_text(str(pid))
        raise PermissionError("prlimit refused")

    monkeypatch.setattr(_ffprobe, "limit_address_space", refuse)

    with pytest.raises(PermissionError):
        run_bounded(
            _python("import time; time.sleep(60)"),
            stdin=DEVNULL,
            timeout=30,
            stdout_limit=1024,
            stderr_limit=1024,
        )

    assert_that(_alive(int(marker.read_text()))).is_false()


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
        _python("import sys; sys.stdout.write('a,1\\nb,2\\n' + 'c' * 4000)"),
        stdin=DEVNULL,
        timeout=30,
        stdout_limit=1_000_000,
        stderr_limit=1024,
        on_stdout_line=lines.append,
    )

    assert_that(lines).is_equal_to([b"a,1", b"b,2", b"c" * 4000])
    assert_that(run.stdout).is_empty()
    assert_that(run.breached).is_false()


@pytest.mark.parametrize("newline", ["", "\\n"])
def test_run_bounded_caps_one_pending_line(newline: str) -> None:
    """A line longer than the cap is an overflow, finished or not (A7)."""
    lines: list[bytes] = []
    started = time.monotonic()

    run = run_bounded(
        _python(
            "import sys, time\n"
            f"sys.stdout.write('x' * 100000 + '{newline}'); sys.stdout.flush()\n"
            "time.sleep(60)",
        ),
        stdin=DEVNULL,
        timeout=60,
        stdout_limit=1 << 30,
        stderr_limit=1024,
        on_stdout_line=lines.append,
        line_limit=4096,
    )

    assert_that(time.monotonic() - started).is_less_than(30)
    assert_that(run.overflowed).is_true()
    assert_that(run.timed_out).is_false()
    assert_that(lines).is_empty()


def test_run_bounded_default_line_cap() -> None:
    """The default cap is a few KiB."""
    run = run_bounded(
        _python("import sys; sys.stdout.write('y' * 5000)"),
        stdin=DEVNULL,
        timeout=30,
        stdout_limit=1 << 20,
        stderr_limit=1024,
        on_stdout_line=lambda line: None,
    )

    assert_that(_ffprobe.LINE_LIMIT_BYTES).is_equal_to(4096)
    assert_that(run.overflowed).is_true()


def test_run_bounded_callback_error_fails_the_call() -> None:
    """A line callback that raises never yields a normal result (A7)."""

    def sink(line: bytes) -> None:
        raise RuntimeError("sink failed")

    with pytest.raises(BoundedRunError) as caught:
        run_bounded(
            _python("print('a'); print('b')"),
            stdin=DEVNULL,
            timeout=30,
            stdout_limit=1024,
            stderr_limit=1024,
            on_stdout_line=sink,
        )

    assert_that(str(caught.value.__cause__)).is_equal_to("sink failed")


def test_run_bounded_callback_error_on_last_line_fails_the_call() -> None:
    """The final line without a newline goes through the same check."""

    def sink(line: bytes) -> None:
        raise ValueError(line)

    with pytest.raises(BoundedRunError):
        run_bounded(
            _python("import sys; sys.stdout.write('tail')"),
            stdin=DEVNULL,
            timeout=30,
            stdout_limit=1024,
            stderr_limit=1024,
            on_stdout_line=sink,
        )


def test_run_bounded_reader_error_kills_the_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing pipe read kills the group and fails the call."""
    pid_file = tmp_path / "pid"
    real_read = os.read
    failing = threading.Event()

    def read(fd: int, size: int) -> bytes:
        if failing.is_set():
            raise OSError("pipe read failed")
        return real_read(fd, size)

    def remember(pid: int) -> None:
        pid_file.write_text(str(pid))
        failing.set()

    monkeypatch.setattr(_ffprobe, "limit_address_space", remember)
    monkeypatch.setattr("winnow.media._ffprobe.os.read", read)
    started = time.monotonic()

    with pytest.raises(BoundedRunError):
        run_bounded(
            _python("import time; time.sleep(60)"),
            stdin=DEVNULL,
            timeout=60,
            stdout_limit=1024,
            stderr_limit=1024,
        )

    assert_that(time.monotonic() - started).is_less_than(30)
    assert_that(_alive(int(pid_file.read_text()))).is_false()


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


def _interrupt_main_thread_after(seconds: float) -> threading.Timer:
    """Start a timer that sends SIGINT to the main thread.

    ``pthread_kill`` targets the main thread, so a blocking call there is
    interrupted the way a terminal Ctrl-C interrupts it.

    Args:
        seconds: Delay before the signal.

    Returns:
        The started timer.
    """
    main = threading.main_thread().ident
    if main is None:
        pytest.fail("the main thread has no ident")
    timer = threading.Timer(seconds, signal.pthread_kill, args=(main, signal.SIGINT))
    timer.start()
    return timer


def _alive(pid: int) -> bool:
    """Report whether a process id still names a running process.

    Args:
        pid: Process id.

    Returns:
        ``True`` while the process exists and is not a zombie we reaped.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    """Wait until a process id no longer exists.

    Args:
        pid: Process id.
        seconds: How long to wait.

    Returns:
        ``True`` when the process is gone in time.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


_GRANDCHILD = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', "
    "'import os, time; print(os.getpid(), flush=True); time.sleep(60)'])\n"
    "{tail}"
)


@pytest.mark.parametrize(
    "tail",
    ["import time; time.sleep(60)\n", "pass\n"],
    ids=["parent-sleeps", "parent-exits"],
)
def test_run_bounded_kills_a_grandchild_holding_the_pipes(tail: str) -> None:
    """A descendant that keeps stdout open is killed with the group (A7).

    In the second case the direct child exits at once, so only the
    grandchild holds the pipes; the run still ends at the time-out.
    """
    lines: list[bytes] = []
    started = time.monotonic()

    run = run_bounded(
        _python(_GRANDCHILD.format(tail=tail)),
        stdin=DEVNULL,
        timeout=1.0,
        stdout_limit=1024,
        stderr_limit=1024,
        on_stdout_line=lines.append,
    )

    elapsed = time.monotonic() - started
    assert_that(run.timed_out).is_true()
    assert_that(elapsed).is_less_than(4)
    assert_that(lines).is_length(1)
    assert_that(_wait_gone(int(lines[0]))).is_true()


def test_run_bounded_interrupt_kills_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt during the wait kills the child before it propagates."""
    pids: list[int] = []
    monkeypatch.setattr(_ffprobe, "limit_address_space", pids.append)
    timer = _interrupt_main_thread_after(0.5)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_bounded(
                _python("import time; time.sleep(60)"),
                stdin=DEVNULL,
                timeout=60,
                stdout_limit=1024,
                stderr_limit=1024,
            )
    finally:
        timer.cancel()

    assert_that(pids).is_length(1)
    assert_that(_alive(pids[0])).is_false()


def test_run_bounded_reader_start_failure_kills_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure while starting the readers kills the child (A7)."""
    pids: list[int] = []
    monkeypatch.setattr(_ffprobe, "limit_address_space", pids.append)
    starts = 0
    real_start = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)

    with pytest.raises(RuntimeError):
        run_bounded(
            _python("import time; time.sleep(60)"),
            stdin=DEVNULL,
            timeout=60,
            stdout_limit=1024,
            stderr_limit=1024,
        )

    assert_that(_alive(pids[0])).is_false()


def test_run_bounded_setup_failure_abandons_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure before supervision starts still kills and reaps the child."""
    spawned: list[object] = []

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("supervisor failed")

    monkeypatch.setattr(_ffprobe, "_spawn", _recording_spawn(spawned, []))
    monkeypatch.setattr(_ffprobe, "_Supervisor", broken)

    with pytest.raises(RuntimeError):
        run_bounded(
            _python("import time; time.sleep(60)"),
            stdin=DEVNULL,
            timeout=60,
            stdout_limit=1024,
            stderr_limit=1024,
        )

    process = spawned[0]
    assert_that(getattr(process, "returncode", None)).is_not_none()


def test_run_command_captures_text(tmp_path: Path) -> None:
    """The video runner returns text output and the exit status."""
    completed = run_command(
        argv=_python("import sys; print('out'); sys.exit(2)"),
        timeout=30,
        operation="probe",
        path=tmp_path,
    )

    assert_that((completed.returncode, completed.stdout)).is_equal_to((2, "out\n"))


def test_run_command_times_out(tmp_path: Path) -> None:
    """A time-out kills the child and raises MediaError."""
    started = time.monotonic()

    with pytest.raises(MediaError):
        run_command(
            argv=_python("import time; time.sleep(60)"),
            timeout=0.5,
            operation="probe",
            path=tmp_path,
        )

    assert_that(time.monotonic() - started).is_less_than(20)


def test_run_command_interrupt_kills_the_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An interrupt during the wait kills the child before it propagates."""
    spawned: list[object] = []
    monkeypatch.setattr(_ffprobe, "_spawn", _recording_spawn(spawned, []))
    timer = _interrupt_main_thread_after(0.5)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_command(
                argv=_python("import time; time.sleep(60)"),
                timeout=60,
                operation="probe",
                path=tmp_path,
            )
    finally:
        timer.cancel()

    assert_that(getattr(spawned[0], "returncode", None)).is_not_none()


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
    """On an unpinned major the ADTS estimate line is unrecognized output."""
    monkeypatch.setenv("STUB_BANNER", "ffprobe version 10.1 Copyright stub")
    monkeypatch.setenv("STUB_RECORDING", str(RECORDED_DIR / "adts.json"))

    metadata = extract_audio_metadata(_audio_file(tmp_path))

    assert_that(metadata).is_equal_to(
        MediaMetadata(audio_flags=(AudioFlag.UNRECOGNIZED_OUTPUT,)),
    )
