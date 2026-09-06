"""``winnow doctor`` environment diagnostics command.

The command inspects the local environment and reports on the pieces Winnow
relies on: the Python runtime, the FFmpeg binary, optional feature extras, the
cache directory, and any discovered configuration file. Results render as a Rich
table and the process exits non-zero when any hard check fails, so the command
is usable both interactively and in CI health checks.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess  # nosec B404 - used only to read local ffmpeg version output
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from winnow.cli.console import StatusLevel, console_from_context, status_text
from winnow.config import find_config_path, load_config
from winnow.exceptions import ConfigError
from winnow.models.config import WinnowConfig

__all__ = [
    "CheckResult",
    "CheckStatus",
    "doctor_command",
    "run_checks",
]

MINIMUM_PYTHON_VERSION = (3, 11)

_OPTIONAL_EXTRAS: dict[str, tuple[str, ...]] = {
    "face": ("face_recognition",),
    "ai-detect": ("onnxruntime",),
}

_FFMPEG_VERSION_TIMEOUT_SECONDS = 10


class CheckStatus(StrEnum):
    """Outcome of a single doctor check."""

    PASS = auto()
    WARN = auto()
    FAIL = auto()


_STATUS_LABELS: dict[CheckStatus, str] = {
    CheckStatus.PASS: "PASS",
    CheckStatus.WARN: "WARN",
    CheckStatus.FAIL: "FAIL",
}

_STATUS_LEVELS: dict[CheckStatus, StatusLevel] = {
    CheckStatus.PASS: StatusLevel.SUCCESS,
    CheckStatus.WARN: StatusLevel.WARNING,
    CheckStatus.FAIL: StatusLevel.ERROR,
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single environment check.

    Args:
        name: Short name of the check, shown in the report.
        status: Outcome of the check.
        detail: Human-readable explanation of the outcome.
    """

    name: str
    status: CheckStatus
    detail: str


def check_python_version(
    version: tuple[int, int, int] | None = None,
) -> CheckResult:
    """Check that the running Python interpreter meets the minimum version.

    Args:
        version: ``(major, minor, micro)`` version to evaluate. Defaults to the
            running interpreter's version when omitted.

    Returns:
        A failing result when the interpreter is older than the minimum
        supported version, otherwise a passing result.
    """
    major, minor, micro = version if version is not None else sys.version_info[:3]
    current_text = f"{major}.{minor}.{micro}"
    minimum_text = ".".join(str(part) for part in MINIMUM_PYTHON_VERSION)
    if (major, minor) < MINIMUM_PYTHON_VERSION:
        return CheckResult(
            name="Python",
            status=CheckStatus.FAIL,
            detail=f"Python {current_text} found; {minimum_text}+ required.",
        )
    return CheckResult(
        name="Python",
        status=CheckStatus.PASS,
        detail=f"Python {current_text}.",
    )


def _ffmpeg_version(executable: str) -> str | None:
    """Read the first line of ``ffmpeg -version`` output.

    Args:
        executable: Resolved path to the FFmpeg binary.

    Returns:
        The version banner line, or ``None`` when it cannot be read.
    """
    try:
        completed = subprocess.run(  # nosec B603 - resolved path, fixed argv, no shell
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=_FFMPEG_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    return first_line or None


def check_ffmpeg() -> CheckResult:
    """Check that the FFmpeg binary is available on the ``PATH``.

    Returns:
        A failing result when FFmpeg is missing, otherwise a passing result that
        includes the detected version banner when it can be read.
    """
    executable = shutil.which("ffmpeg")
    if executable is None:
        return CheckResult(
            name="FFmpeg",
            status=CheckStatus.FAIL,
            detail="ffmpeg not found on PATH; install it to process video.",
        )
    version_line = _ffmpeg_version(executable)
    detail = version_line if version_line is not None else f"found at {executable}."
    return CheckResult(name="FFmpeg", status=CheckStatus.PASS, detail=detail)


def check_optional_extras() -> list[CheckResult]:
    """Check whether optional feature extras are importable.

    Returns:
        One warning-or-pass result per optional extra. Missing extras warn rather
        than fail because they are opt-in features.
    """
    results: list[CheckResult] = []
    for extra, modules in _OPTIONAL_EXTRAS.items():
        missing = [
            module for module in modules if importlib.util.find_spec(module) is None
        ]
        if missing:
            results.append(
                CheckResult(
                    name=f"Extra: {extra}",
                    status=CheckStatus.WARN,
                    detail=(
                        f"not installed (optional); "
                        f"install with 'pip install winnow-media[{extra}]'."
                    ),
                ),
            )
            continue
        results.append(
            CheckResult(
                name=f"Extra: {extra}",
                status=CheckStatus.PASS,
                detail="installed.",
            ),
        )
    return results


def _directory_is_writable(directory: Path) -> bool:
    """Report whether a directory can be written to.

    Args:
        directory: Existing directory to probe.

    Returns:
        ``True`` when the process has write access to the directory.
    """
    return os.access(directory, os.W_OK)


def check_cache_dir(config: WinnowConfig) -> CheckResult:
    """Check that the configured cache directory is usable.

    Args:
        config: Loaded configuration whose cache directory is inspected.

    Returns:
        A passing result when the cache directory exists and is writable, a
        passing result when it is absent but its parent is writable, or a warning
        when neither the directory nor its parent can be written.
    """
    cache_dir = config.cache.directory
    if cache_dir.exists():
        if _directory_is_writable(cache_dir):
            return CheckResult(
                name="Cache directory",
                status=CheckStatus.PASS,
                detail=f"{cache_dir} is writable.",
            )
        return CheckResult(
            name="Cache directory",
            status=CheckStatus.WARN,
            detail=f"{cache_dir} exists but is not writable.",
        )
    parent = next(
        (ancestor for ancestor in cache_dir.parents if ancestor.exists()),
        cache_dir.parent,
    )
    if parent.exists() and _directory_is_writable(parent):
        return CheckResult(
            name="Cache directory",
            status=CheckStatus.PASS,
            detail=f"{cache_dir} is absent but can be created.",
        )
    return CheckResult(
        name="Cache directory",
        status=CheckStatus.WARN,
        detail=f"{cache_dir} cannot be created; check parent permissions.",
    )


def _load_config_for_checks() -> tuple[WinnowConfig, CheckResult]:
    """Load configuration and describe the outcome for the report.

    Returns:
        A tuple of the configuration to use for later checks and the config
        check result. Defaults are returned when no config file is present or
        when loading fails, so downstream checks can still run.
    """
    config_path = find_config_path()
    if config_path is None:
        return WinnowConfig(), CheckResult(
            name="Configuration",
            status=CheckStatus.PASS,
            detail="no config file found; using defaults.",
        )
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return WinnowConfig(), CheckResult(
            name="Configuration",
            status=CheckStatus.FAIL,
            detail=f"{config_path} is invalid: {exc.message}",
        )
    return config, CheckResult(
        name="Configuration",
        status=CheckStatus.PASS,
        detail=f"{config_path} is valid.",
    )


def run_checks() -> list[CheckResult]:
    """Run every doctor check in report order.

    Returns:
        Ordered results for the Python runtime, FFmpeg, configuration, cache
        directory, and each optional extra.
    """
    config, config_result = _load_config_for_checks()
    results = [
        check_python_version(),
        check_ffmpeg(),
        config_result,
        check_cache_dir(config),
    ]
    results.extend(check_optional_extras())
    return results


def _build_report_table(results: Iterable[CheckResult]) -> Table:
    """Build the Rich table summarizing check results.

    Args:
        results: Results to render, in display order.

    Returns:
        A populated Rich table with status, check, and detail columns.
    """
    table = Table(title="Winnow environment diagnostics")
    table.add_column("Status", no_wrap=True)
    table.add_column("Check", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    for result in results:
        table.add_row(
            status_text(_STATUS_LABELS[result.status], _STATUS_LEVELS[result.status]),
            result.name,
            result.detail,
        )
    return table


def render_report(console: Console, results: Iterable[CheckResult]) -> None:
    """Render the diagnostics report to a console.

    Args:
        console: Console used to render the table.
        results: Check results to display.
    """
    console.print(_build_report_table(results))


@click.command(name="doctor")
@click.pass_context
def doctor_command(ctx: click.Context) -> None:
    """Diagnose the local environment for running Winnow.

    Args:
        ctx: Active Click context carrying root option state.

    Raises:
        click.exceptions.Exit: With a non-zero code when any hard check fails;
            raised by :meth:`click.Context.exit`.
    """
    console = console_from_context(ctx)
    results = run_checks()
    render_report(console, results)
    if any(result.status is CheckStatus.FAIL for result in results):
        ctx.exit(1)
