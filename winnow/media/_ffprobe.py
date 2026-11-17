"""Shared runner for the FFmpeg command-line tools.

Both the audio and the video readers start ``ffprobe`` (and the video reader
``ffmpeg``) through this module. Every command is a fixed argument vector with
an absolute binary path from :func:`shutil.which`; nothing goes through a
shell.

Two runners live here:

* :func:`run_command` captures text output with a time-out. The video reader
  uses it.
* :func:`run_bounded` is the guarded runner the audio reader uses. It bounds
  the run time and the bytes read from stdout and stderr, kills and reaps the
  process on any breach, can hand stdout to a callback line by line as it
  arrives, and on Linux caps the child's address space.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv lists only; never shell=True
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import IO, Final

from winnow.exceptions import MediaError

DEVNULL: Final[int] = subprocess.DEVNULL
"""Stdin value for runs that read no file through stdin."""

FFPROBE: Final[str] = "ffprobe"
FFMPEG: Final[str] = "ffmpeg"

TESTED_FFPROBE_MAJORS: Final[frozenset[int]] = frozenset({9})
"""FFmpeg major versions the audio rules were measured on."""

ADDRESS_SPACE_LIMIT_BYTES: Final[int] = 1024**3
"""``RLIMIT_AS`` applied to guarded runs on Linux."""

_VERSION_TIMEOUT_SECONDS: Final[float] = 10.0
_VERSION_OUTPUT_LIMIT: Final[int] = 1024 * 1024
_READ_CHUNK: Final[int] = 64 * 1024
_REAP_TIMEOUT_SECONDS: Final[float] = 5.0
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^ffprobe version n?(?P<major>[0-9]+)\.[0-9]+",
)


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
        return subprocess.run(  # nosec B603 - fixed argv list from which(); no shell
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
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


def limit_address_space() -> None:
    """Cap the address space of the current process at 1 GiB.

    Runs in the child between ``fork`` and ``exec`` on Linux. A lower hard
    limit already in force is kept.
    """
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = ADDRESS_SPACE_LIMIT_BYTES
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))


def _preexec() -> Callable[[], None] | None:
    """Return the pre-exec hook for the running platform.

    Returns:
        :func:`limit_address_space` on Linux, otherwise ``None``.
    """
    return limit_address_space if sys.platform == "linux" else None


@dataclass(frozen=True, slots=True)
class BoundedRun:
    """Outcome of :func:`run_bounded`.

    Args:
        returncode: Exit status after reaping; negative when killed.
        stdout: Captured stdout; empty when a line callback consumed it.
        stderr: Captured stderr.
        timed_out: ``True`` when the time-out expired.
        overflowed: ``True`` when stdout or stderr exceeded its bound.
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


class _LineSplitter:
    """Hand complete lines of a byte stream to a callback."""

    def __init__(self, callback: Callable[[bytes], None]) -> None:
        self._callback = callback
        self._pending = b""

    def feed(self, chunk: bytes) -> None:
        """Split ``chunk`` into lines, keeping any partial tail.

        Args:
            chunk: Bytes just read.
        """
        data = self._pending + chunk
        *lines, self._pending = data.split(b"\n")
        for line in lines:
            self._callback(line)

    def close(self) -> None:
        """Deliver a final line that had no newline."""
        if self._pending:
            self._callback(self._pending)
            self._pending = b""


def _pump(
    *,
    stream: IO[bytes],
    limit: int,
    sink: Callable[[bytes], None],
    overflow: threading.Event,
    kill: Callable[[], None],
) -> None:
    """Copy a pipe into ``sink`` until EOF or until ``limit`` bytes pass.

    Args:
        stream: Pipe to read.
        limit: Maximum number of bytes accepted.
        sink: Receives each chunk read within the limit.
        overflow: Set when the limit is exceeded.
        kill: Kills the process on overflow.
    """
    total = 0
    while True:
        chunk = os.read(stream.fileno(), _READ_CHUNK)
        if not chunk:
            return
        total += len(chunk)
        if total > limit:
            overflow.set()
            kill()
            return
        sink(chunk)


def _pipe(stream: IO[bytes] | None) -> IO[bytes]:
    """Return a process pipe that :data:`subprocess.PIPE` must have created.

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


def run_bounded(
    argv: list[str],
    *,
    stdin: int,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    on_stdout_line: Callable[[bytes], None] | None = None,
) -> BoundedRun:
    """Run a guarded ffprobe command.

    The process gets the :func:`probe_environment`, its stdin is ``stdin``,
    and both output pipes are read concurrently. When the time-out expires or
    either pipe passes its bound, the process is killed and reaped; it is
    never retried.

    Args:
        argv: Fixed argument vector with an absolute binary path.
        stdin: File descriptor (or :data:`subprocess.DEVNULL`) for stdin.
        timeout: Maximum run time in seconds.
        stdout_limit: Maximum stdout bytes.
        stderr_limit: Maximum stderr bytes.
        on_stdout_line: When given, receives each stdout line (without the
            newline) as it arrives and stdout is not kept.

    Returns:
        The exit status, captured output, and breach flags.

    Raises:
        OSError: If the process cannot be started.
    """
    process = subprocess.Popen(  # nosec B603 - fixed argv list from which(); no shell
        argv,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=probe_environment(),
        preexec_fn=_preexec(),
        close_fds=True,
    )
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    overflow = threading.Event()

    def kill() -> None:
        with contextlib.suppress(OSError):
            process.kill()

    splitter = _LineSplitter(on_stdout_line) if on_stdout_line is not None else None
    stdout_sink = splitter.feed if splitter is not None else stdout_parts.append
    stdout_pipe = _pipe(process.stdout)
    stderr_pipe = _pipe(process.stderr)
    readers = [
        threading.Thread(
            target=_pump,
            kwargs={
                "stream": stdout_pipe,
                "limit": stdout_limit,
                "sink": stdout_sink,
                "overflow": overflow,
                "kill": kill,
            },
            daemon=True,
        ),
        threading.Thread(
            target=_pump,
            kwargs={
                "stream": stderr_pipe,
                "limit": stderr_limit,
                "sink": stderr_parts.append,
                "overflow": overflow,
                "kill": kill,
            },
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill()
    returncode = process.wait()
    for reader in readers:
        reader.join(timeout=_REAP_TIMEOUT_SECONDS)
    stdout_pipe.close()
    stderr_pipe.close()
    if splitter is not None and not overflow.is_set():
        splitter.close()
    return BoundedRun(
        returncode=returncode,
        stdout=b"".join(stdout_parts),
        stderr=b"".join(stderr_parts),
        timed_out=timed_out,
        overflowed=overflow.is_set(),
    )


def parse_version_major(banner: str) -> int | None:
    """Read the major version from an ``ffprobe -version`` banner line.

    Args:
        banner: First line of ``ffprobe -version``.

    Returns:
        The major version, or ``None`` for builds without a release number.
    """
    match = _VERSION_PATTERN.match(banner)
    return int(match.group("major")) if match is not None else None


@lru_cache(maxsize=8)
def ffprobe_version(binary: str) -> str | None:
    """Return the first line of ``ffprobe -version``.

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


@lru_cache(maxsize=8)
def fd_protocol_supported(binary: str) -> bool:
    """Report whether ffprobe lists ``fd`` among its input protocols.

    Args:
        binary: Absolute path of the ffprobe binary.

    Returns:
        ``True`` when the ``fd`` input protocol is available.
    """
    run = _run_info(binary, "-protocols")
    if run is None:
        return False
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
    except OSError:
        return None
    if run.breached or run.returncode != 0:
        return None
    return run.stdout


__all__ = [
    "ADDRESS_SPACE_LIMIT_BYTES",
    "DEVNULL",
    "FFMPEG",
    "FFPROBE",
    "TESTED_FFPROBE_MAJORS",
    "BoundedRun",
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
