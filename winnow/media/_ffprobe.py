"""Shared runner for the FFmpeg command-line tools.

Both the audio and the video readers start ``ffprobe`` (and the video reader
``ffmpeg``) through this module. Every command is a fixed argument vector with
an absolute binary path from :func:`shutil.which`; nothing goes through a
shell, and every process is started by :func:`_spawn`.

Two runners live here:

* :func:`run_command` captures text output with a time-out. The video reader
  uses it.
* :func:`run_bounded` is the guarded runner the audio reader uses. The child
  runs in its own process group (its own session on POSIX); the run time,
  the bytes read from stdout and stderr, and the length of one stdout line
  are bounded, and on any breach the whole group is killed and the child
  reaped. Stdout can go to a callback line by line as it arrives. On Linux
  the child's address space is capped from the parent with ``prlimit``. All
  work after the child starts runs under cleanup that kills, reaps, and
  closes the pipes on any exception, including an interrupt; an error in a
  pipe reader or in the line callback fails the call.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess  # nosec B404 - fixed argv lists only; never shell=True
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from winnow.exceptions import MediaError

DEVNULL: Final[int] = subprocess.DEVNULL
"""Stdin value for runs that read no file through stdin."""

FFPROBE: Final[str] = "ffprobe"
FFMPEG: Final[str] = "ffmpeg"

TESTED_FFPROBE_MAJORS: Final[frozenset[int]] = frozenset({9})
"""FFmpeg major versions the audio rules were measured on."""

ADDRESS_SPACE_LIMIT_BYTES: Final[int] = 1024**3
"""``RLIMIT_AS`` applied to guarded runs on Linux."""

LINE_LIMIT_BYTES: Final[int] = 4096
"""Longest stdout line :func:`run_bounded` hands to a line callback."""

_VERSION_TIMEOUT_SECONDS: Final[float] = 10.0
_VERSION_OUTPUT_LIMIT: Final[int] = 1024 * 1024
_READ_CHUNK: Final[int] = 64 * 1024
_REAP_TIMEOUT_SECONDS: Final[float] = 5.0
_UNREAPED_RETURNCODE: Final[int] = -9
"""Exit status reported for a killed child that was not reaped in time.

The number of SIGKILL on POSIX, written as a literal because ``signal`` has
no ``SIGKILL`` on Windows and this module must still import there.
"""
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^ffprobe version (?:[0-9]+:)?n?(?P<major>[0-9]+)\.[0-9]+",
)
"""Release banner, with the Debian/Ubuntu epoch prefix allowed (``7:9.0.2-1``)."""


class BoundedRunError(RuntimeError):
    """A pipe reader or the line callback of :func:`run_bounded` failed.

    The run's output is incomplete, so no result is returned; the original
    exception is the ``__cause__``.
    """


def find_binary(name: str) -> str | None:
    """Resolve a tool on ``PATH`` to an absolute path.

    Args:
        name: Executable name, for example ``ffprobe``.

    Returns:
        The absolute path, or ``None`` when the tool is not on ``PATH``.
    """
    found = shutil.which(name)
    if found is None:
        return None
    return str(Path(found).absolute())


def ffprobe_available() -> bool:
    """Report whether the ``ffprobe`` binary is on ``PATH``.

    Returns:
        ``True`` when ffprobe can be resolved.
    """
    return find_binary(FFPROBE) is not None


def ffmpeg_available() -> bool:
    """Report whether the ``ffmpeg`` binary is on ``PATH``.

    Returns:
        ``True`` when ffmpeg can be resolved.
    """
    return find_binary(FFMPEG) is not None


def _spawn(
    argv: list[str],
    *,
    stdin: int | None,
    text: bool,
    env: Mapping[str, str] | None = None,
    isolate: bool = False,
) -> subprocess.Popen[Any]:
    """Start a process with both output pipes; the one place a process starts.

    Args:
        argv: Fixed argument vector; ``argv[0]`` is an absolute binary path.
        stdin: Descriptor, :data:`DEVNULL`, or ``None`` to inherit stdin.
        text: Decode the output pipes as text.
        env: Environment for the child; ``None`` inherits the parent's.
        isolate: Start the child in its own process group (a new session on
            POSIX, a new process group on Windows).

    Returns:
        The started process.
    """
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(  # nosec B603 - fixed argv list from which(); no shell
        argv,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=text,
        close_fds=True,
        start_new_session=isolate and os.name == "posix",
        creationflags=new_group if isolate and os.name == "nt" else 0,
    )


def _capture(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a command to completion and capture its text output.

    Behaves like ``subprocess.run(argv, capture_output=True, text=True,
    check=False, timeout=timeout)``: on a time-out the process is killed and
    reaped and :class:`subprocess.TimeoutExpired` is raised. Any other
    exception, an interrupt included, also kills and reaps it first.

    Args:
        argv: Fixed argument vector with an absolute binary path.
        timeout: Maximum seconds to wait.

    Returns:
        The completed process.

    Raises:
        subprocess.TimeoutExpired: If the time-out expires.
    """
    with _spawn(argv, stdin=None, text=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except BaseException:
            process.kill()
            process.wait()
            raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def run_command(
    *,
    argv: list[str],
    timeout: float,
    operation: str,
    path: Path,
) -> subprocess.CompletedProcess[str]:
    """Run an FFmpeg-family command with a fixed argument vector.

    Args:
        argv: Fully resolved command and arguments; ``argv[0]`` is an absolute
            binary path from :func:`find_binary`.
        timeout: Maximum seconds to wait for completion.
        operation: Operation name used in raised errors.
        path: Media path used in raised errors.

    Returns:
        The completed process, including captured stdout and stderr.

    Raises:
        MediaError: If the process cannot be launched or times out.
    """
    try:
        return _capture(argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            "FFmpeg command timed out",
            operation=operation,
            file_path=path,
        ) from exc
    except OSError as exc:
        raise MediaError(
            "failed to launch FFmpeg command",
            operation=operation,
            file_path=path,
        ) from exc


def probe_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the environment for a guarded ffprobe run.

    ``FFREPORT`` is removed so ffprobe writes no report file, colour codes are
    switched off, and the locale is fixed.

    Args:
        base: Environment to start from; defaults to :data:`os.environ`.

    Returns:
        A new environment mapping.
    """
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items() if key != "FFREPORT"}
    env["AV_LOG_FORCE_NOCOLOR"] = "1"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def limit_address_space(pid: int) -> None:
    """Cap a child's address space at 1 GiB, on Linux only.

    The limit is set from the parent with ``prlimit`` on the child's pid, so
    no code runs in the child between ``fork`` and ``exec``. A lower soft or
    hard limit already in force is kept, never raised. A child that has
    already exited needs no limit.

    Args:
        pid: Process id of the child.
    """
    if sys.platform != "linux":
        return
    import resource

    try:
        soft, hard = resource.prlimit(pid, resource.RLIMIT_AS)
        limit = ADDRESS_SPACE_LIMIT_BYTES
        for current in (soft, hard):
            if current != resource.RLIM_INFINITY:
                limit = min(limit, current)
        resource.prlimit(pid, resource.RLIMIT_AS, (limit, hard))
    except ProcessLookupError:
        return


@dataclass(frozen=True, slots=True)
class BoundedRun:
    """Outcome of :func:`run_bounded`.

    Args:
        returncode: Exit status after reaping; negative when killed.
        stdout: Captured stdout; empty when a line callback consumed it.
        stderr: Captured stderr.
        timed_out: ``True`` when the time-out expired.
        overflowed: ``True`` when stdout, stderr, or one stdout line
            exceeded its bound.
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    overflowed: bool

    @property
    def breached(self) -> bool:
        """Report whether the run hit a time or output bound.

        Returns:
            ``True`` when the process was killed for a breach.
        """
        return self.timed_out or self.overflowed


class _BoundExceededError(Exception):
    """Raised when a pipe passes its byte bound or a line its length bound."""


class _LineSplitter:
    """Hand complete lines of a byte stream to a callback.

    Args:
        callback: Receives each line without its newline.
        limit: Longest line accepted, in bytes.
    """

    def __init__(self, callback: Callable[[bytes], None], limit: int) -> None:
        self._callback = callback
        self._limit = limit
        self._pending = b""

    def feed(self, chunk: bytes) -> None:
        """Split ``chunk`` into lines, keeping any partial tail.

        Args:
            chunk: Bytes just read.

        Raises:
            _BoundExceededError: If a line, finished or not, passes the limit.
        """
        data = self._pending + chunk
        *lines, pending = data.split(b"\n")
        if len(pending) > self._limit or any(len(x) > self._limit for x in lines):
            raise _BoundExceededError
        self._pending = pending
        for line in lines:
            self._callback(line)

    def close(self) -> None:
        """Deliver a final line that had no newline."""
        if self._pending:
            line, self._pending = self._pending, b""
            self._callback(line)


def _pipe(stream: IO[bytes] | None) -> IO[bytes]:
    """Return a process pipe that :func:`_spawn` must have created.

    Args:
        stream: ``Popen.stdout`` or ``Popen.stderr``.

    Returns:
        The pipe.

    Raises:
        OSError: If the pipe is missing.
    """
    if stream is None:
        raise OSError("ffprobe started without an output pipe")
    return stream


class _Supervisor:
    """Read a guarded child's pipes, enforce the bounds, and clean up.

    Args:
        process: The started child, in its own process group.
        stdout_limit: Maximum stdout bytes.
        stderr_limit: Maximum stderr bytes.
        on_stdout_line: Line callback, or ``None`` to keep stdout.
        line_limit: Longest stdout line for the callback.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        stdout_limit: int,
        stderr_limit: int,
        on_stdout_line: Callable[[bytes], None] | None,
        line_limit: int,
    ) -> None:
        self._process = process
        self._stdout_parts: list[bytes] = []
        self._stderr_parts: list[bytes] = []
        self._overflow = threading.Event()
        self._errors: list[BaseException] = []
        self._splitter = (
            _LineSplitter(on_stdout_line, line_limit)
            if on_stdout_line is not None
            else None
        )
        stdout_sink = (
            self._splitter.feed
            if self._splitter is not None
            else self._stdout_parts.append
        )
        self._pipes = (_pipe(process.stdout), _pipe(process.stderr))
        self._readers = [
            threading.Thread(
                target=self._pump,
                args=(self._pipes[0], stdout_limit, stdout_sink),
                daemon=True,
            ),
            threading.Thread(
                target=self._pump,
                args=(self._pipes[1], stderr_limit, self._stderr_parts.append),
                daemon=True,
            ),
        ]

    def run(self, timeout: float) -> BoundedRun:
        """Read both pipes until EOF, the deadline, or a breach.

        Args:
            timeout: Maximum seconds for the child to exit and both pipes to
                reach EOF.

        Returns:
            The run's outcome.

        Raises:
            BoundedRunError: If a reader or the line callback failed.
        """
        deadline = time.monotonic() + timeout
        limit_address_space(self._process.pid)
        for reader in self._readers:
            reader.start()
        timed_out = not self._finished_by(deadline)
        if timed_out:
            self.kill()
        self._join_readers(_REAP_TIMEOUT_SECONDS)
        if any(reader.is_alive() for reader in self._readers):
            timed_out = True
        try:
            returncode = self._process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = _UNREAPED_RETURNCODE
        if self._errors:
            raise BoundedRunError("ffprobe output could not be read") from (
                self._errors[0]
            )
        overflowed = self._overflow.is_set()
        if self._splitter is not None and not (timed_out or overflowed):
            try:
                self._splitter.close()
            except Exception as exc:
                raise BoundedRunError("ffprobe output could not be read") from exc
        return BoundedRun(
            returncode=returncode,
            stdout=b"".join(self._stdout_parts),
            stderr=b"".join(self._stderr_parts),
            timed_out=timed_out,
            overflowed=overflowed,
        )

    def cleanup(self) -> None:
        """Kill the group while anything may run, reap it, close the pipes.

        The group is killed when the child is not yet reaped and also when
        it is but a reader still waits: then a descendant holds a pipe, as
        when an interrupt arrives during the wait for the readers.

        A pipe still held by a reader thread that did not finish is left
        open: closing it could hand its descriptor number to another file
        while the thread still reads from it.
        """
        if self._process.returncode is None or any(
            reader.is_alive() for reader in self._readers
        ):
            self.kill()
        if self._process.returncode is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        self._join_readers(_REAP_TIMEOUT_SECONDS)
        for pipe, reader in zip(self._pipes, self._readers, strict=True):
            if not reader.is_alive():
                pipe.close()

    def kill(self) -> None:
        """Kill the child's process group, or the child when it has none.

        The group is signalled only while it is known to exist: while the
        child is not yet reaped, or while a reader still waits for EOF from
        a group member that holds a pipe.
        """
        process = self._process
        alive = process.returncode is None or any(
            reader.is_alive() for reader in self._readers
        )
        if os.name == "posix" and alive:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            process.kill()

    def _finished_by(self, deadline: float) -> bool:
        """Wait for the child to exit and both pipes to reach EOF.

        Args:
            deadline: :func:`time.monotonic` value to stop at.

        Returns:
            ``True`` when everything finished in time.
        """
        try:
            self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
        for reader in self._readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(reader.is_alive() for reader in self._readers)

    def _join_readers(self, timeout: float) -> None:
        """Join the reader threads that were started.

        Args:
            timeout: Seconds to wait for each.
        """
        for reader in self._readers:
            if reader.ident is not None:
                reader.join(timeout=timeout)

    def _pump(
        self,
        stream: IO[bytes],
        limit: int,
        sink: Callable[[bytes], None],
    ) -> None:
        """Copy a pipe into ``sink`` until EOF or a breach.

        Overflow of the byte or line bound sets the overflow flag; any other
        exception is kept for :meth:`run` to raise. Both kill the group.

        Args:
            stream: Pipe to read.
            limit: Maximum number of bytes accepted.
            sink: Receives each chunk read within the limit.
        """
        total = 0
        try:
            while True:
                chunk = os.read(stream.fileno(), _READ_CHUNK)
                if not chunk:
                    return
                total += len(chunk)
                if total > limit:
                    raise _BoundExceededError
                sink(chunk)
        except _BoundExceededError:
            self._overflow.set()
            self.kill()
        except BaseException as exc:
            self._errors.append(exc)
            self.kill()


def run_bounded(
    argv: list[str],
    *,
    stdin: int,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    on_stdout_line: Callable[[bytes], None] | None = None,
    line_limit: int = LINE_LIMIT_BYTES,
) -> BoundedRun:
    """Run a guarded ffprobe command.

    The process gets the :func:`probe_environment`, its stdin is ``stdin``,
    it runs in its own process group, and both output pipes are read
    concurrently. When the time-out expires (the child must exit and both
    pipes reach EOF within it) or a pipe or line passes its bound, the whole
    group is killed and the child reaped; it is never retried. Any exception
    after the start, including :class:`KeyboardInterrupt`, kills and reaps
    the child and closes the pipes before it propagates.

    Args:
        argv: Fixed argument vector with an absolute binary path.
        stdin: File descriptor (or :data:`DEVNULL`) for stdin.
        timeout: Maximum run time in seconds.
        stdout_limit: Maximum stdout bytes.
        stderr_limit: Maximum stderr bytes.
        on_stdout_line: When given, receives each stdout line (without the
            newline) as it arrives and stdout is not kept.
        line_limit: Longest stdout line passed to ``on_stdout_line``.

    Returns:
        The exit status, captured output, and breach flags. A child that is
        not reaped in time after the kill is reported as ``timed_out``.

    Raises:
        OSError: If the process cannot be started or limited.
        BoundedRunError: If a pipe reader or ``on_stdout_line`` failed.
    """
    process = _spawn(
        argv,
        stdin=stdin,
        text=False,
        env=probe_environment(),
        isolate=True,
    )
    supervisor: _Supervisor | None = None
    try:
        supervisor = _Supervisor(
            process,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            on_stdout_line=on_stdout_line,
            line_limit=line_limit,
        )
        return supervisor.run(timeout)
    finally:
        if supervisor is not None:
            supervisor.cleanup()
        else:
            _abandon(process)


def _abandon(process: subprocess.Popen[bytes]) -> None:
    """Kill, reap, and close a child whose supervision never started.

    Args:
        process: The started child.
    """
    if os.name == "posix" and process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def parse_version_major(banner: str) -> int | None:
    """Read the major version from an ``ffprobe -version`` banner line.

    Args:
        banner: First line of ``ffprobe -version``.

    Returns:
        The major version, or ``None`` for builds without a release number.
    """
    match = _VERSION_PATTERN.match(banner)
    return int(match.group("major")) if match is not None else None


_CACHE_SIZE: Final[int] = 8


class _SuccessCache:
    """Remember the successful answers of an info probe, per binary.

    A failed probe (``None``) is never stored, so the next call asks
    ffprobe again instead of keeping a failure for the life of the process.

    Args:
        probe: Returns the answer for a binary, or ``None`` when ffprobe
            could not answer.
    """

    def __init__(self, probe: Callable[[str], Any]) -> None:
        self._probe = probe
        self._answers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, binary: str) -> Any:
        """Return the cached answer, probing when there is none.

        Args:
            binary: Absolute path of the ffprobe binary.

        Returns:
            The answer, or ``None`` when the probe failed.
        """
        with self._lock:
            if binary in self._answers:
                return self._answers[binary]
        answer = self._probe(binary)
        if answer is not None:
            with self._lock:
                if len(self._answers) >= _CACHE_SIZE:
                    self._answers.clear()
                self._answers[binary] = answer
        return answer

    def clear(self) -> None:
        """Forget every answer."""
        with self._lock:
            self._answers.clear()


def _read_version(binary: str) -> str | None:
    """Run ``ffprobe -version`` and return its first line.

    Args:
        binary: Absolute path of the ffprobe binary.

    Returns:
        The banner line, or ``None`` when it cannot be read.
    """
    run = _run_info(binary, "-version")
    if run is None:
        return None
    lines = run.decode("utf-8", errors="replace").splitlines()
    return lines[0].strip() if lines and lines[0].strip() else None


_VERSIONS: Final[_SuccessCache] = _SuccessCache(_read_version)


def ffprobe_version(binary: str) -> str | None:
    """Return the first line of ``ffprobe -version``.

    A banner that was read is cached per binary; a failure is not (A12c).

    Args:
        binary: Absolute path of the ffprobe binary.

    Returns:
        The banner line, or ``None`` when it cannot be read.
    """
    answer: str | None = _VERSIONS.get(binary)
    return answer


def _read_fd_protocol(binary: str) -> bool | None:
    """Run ``ffprobe -protocols`` and look for the ``fd`` input protocol.

    Args:
        binary: Absolute path of the ffprobe binary.

    Returns:
        Whether ``fd`` is an input protocol, or ``None`` when the run failed.
    """
    run = _run_info(binary, "-protocols")
    if run is None:
        return None
    in_inputs = False
    for raw in run.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line == "Input:":
            in_inputs = True
        elif line == "Output:":
            in_inputs = False
        elif in_inputs and line == "fd":
            return True
    return False


_FD_PROTOCOL: Final[_SuccessCache] = _SuccessCache(_read_fd_protocol)


def fd_protocol_supported(binary: str) -> bool:
    """Report whether ffprobe lists ``fd`` among its input protocols.

    A successful answer is cached per binary; a failed run is not (A12c)
    and counts as ``False`` for this call.

    Args:
        binary: Absolute path of the ffprobe binary.

    Returns:
        ``True`` when the ``fd`` input protocol is available.
    """
    return _FD_PROTOCOL.get(binary) is True


def clear_info_caches() -> None:
    """Forget every cached ``-version`` and ``-protocols`` answer."""
    _VERSIONS.clear()
    _FD_PROTOCOL.clear()


def _run_info(binary: str, flag: str) -> bytes | None:
    """Run ``ffprobe -hide_banner <flag>`` and return its stdout.

    Args:
        binary: Absolute path of the ffprobe binary.
        flag: ``-version`` or ``-protocols``.

    Returns:
        Stdout bytes, or ``None`` when the run failed or breached a bound.
    """
    argv = [binary, "-hide_banner", flag] if flag != "-version" else [binary, flag]
    try:
        run = run_bounded(
            argv,
            stdin=DEVNULL,
            timeout=_VERSION_TIMEOUT_SECONDS,
            stdout_limit=_VERSION_OUTPUT_LIMIT,
            stderr_limit=_VERSION_OUTPUT_LIMIT,
        )
    except (OSError, BoundedRunError):
        return None
    if run.breached or run.returncode != 0:
        return None
    return run.stdout


__all__ = [
    "ADDRESS_SPACE_LIMIT_BYTES",
    "DEVNULL",
    "FFMPEG",
    "FFPROBE",
    "LINE_LIMIT_BYTES",
    "TESTED_FFPROBE_MAJORS",
    "BoundedRun",
    "BoundedRunError",
    "clear_info_caches",
    "fd_protocol_supported",
    "ffmpeg_available",
    "ffprobe_available",
    "ffprobe_version",
    "find_binary",
    "limit_address_space",
    "parse_version_major",
    "probe_environment",
    "run_bounded",
    "run_command",
]
