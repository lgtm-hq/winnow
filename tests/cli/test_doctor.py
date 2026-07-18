"""Tests for the ``winnow doctor`` command and its checks."""

from __future__ import annotations

from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.commands import doctor as doctor_module
from winnow.cli.commands.doctor import (
    CheckResult,
    CheckStatus,
    check_cache_dir,
    check_ffmpeg,
    check_optional_extras,
    check_python_version,
    run_checks,
)
from winnow.exceptions import ConfigError
from winnow.models.config import CacheSettings, WinnowConfig


def _config_with_cache_dir(directory: Path) -> WinnowConfig:
    """Build a config whose cache directory points at ``directory``.

    Args:
        directory: Cache directory to embed in the configuration.

    Returns:
        A configuration with the requested cache directory.
    """
    return WinnowConfig(cache=CacheSettings(directory=directory))


def test_check_python_version_passes_for_supported_version() -> None:
    """A supported interpreter version passes the Python check."""
    result = check_python_version(version=(3, 11, 0))
    assert_that(result.status).is_equal_to(CheckStatus.PASS)


def test_check_python_version_fails_for_old_version() -> None:
    """An interpreter older than the minimum fails the Python check."""
    result = check_python_version(version=(3, 10, 12))
    assert_that(result.status).is_equal_to(CheckStatus.FAIL)
    assert_that(result.detail).contains("3.11")


def test_check_ffmpeg_passes_when_binary_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FFmpeg check passes and reports the version banner when present."""
    monkeypatch.setattr(
        "winnow.cli.commands.doctor.shutil.which",
        lambda _: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        doctor_module,
        "_ffmpeg_version",
        lambda _: "ffmpeg version 6.1.1",
    )
    result = check_ffmpeg()
    assert_that(result.status).is_equal_to(CheckStatus.PASS)
    assert_that(result.detail).contains("6.1.1")


def test_check_ffmpeg_passes_without_version_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FFmpeg check falls back to the path when no banner is readable."""
    monkeypatch.setattr(
        "winnow.cli.commands.doctor.shutil.which",
        lambda _: "/opt/ffmpeg",
    )
    monkeypatch.setattr(doctor_module, "_ffmpeg_version", lambda _: None)
    result = check_ffmpeg()
    assert_that(result.status).is_equal_to(CheckStatus.PASS)
    assert_that(result.detail).contains("/opt/ffmpeg")


def test_check_ffmpeg_fails_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """FFmpeg check fails when the binary is not on the PATH."""
    monkeypatch.setattr("winnow.cli.commands.doctor.shutil.which", lambda _: None)
    result = check_ffmpeg()
    assert_that(result.status).is_equal_to(CheckStatus.FAIL)


def test_ffmpeg_version_reads_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The version helper returns the first line of ffmpeg output."""

    class _Completed:
        """Stub completed process exposing stdout."""

        stdout = "ffmpeg version 7.0\nbuilt with gcc\n"

    monkeypatch.setattr(
        "winnow.cli.commands.doctor.subprocess.run",
        lambda *args, **kwargs: _Completed(),
    )
    assert_that(doctor_module._ffmpeg_version("/usr/bin/ffmpeg")).is_equal_to(
        "ffmpeg version 7.0",
    )


def test_ffmpeg_version_returns_none_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version helper returns ``None`` when the subprocess fails."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr("winnow.cli.commands.doctor.subprocess.run", _raise)
    assert_that(doctor_module._ffmpeg_version("/usr/bin/ffmpeg")).is_none()


def test_check_optional_extras_warns_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing optional extras produce warnings rather than failures."""
    monkeypatch.setattr(
        "winnow.cli.commands.doctor.importlib.util.find_spec",
        lambda _: None,
    )
    results = check_optional_extras()
    assert_that(results).is_length(len(doctor_module._OPTIONAL_EXTRAS))
    statuses = {result.status for result in results}
    assert_that(statuses).is_equal_to({CheckStatus.WARN})


def test_check_optional_extras_passes_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importable optional extras pass their checks."""
    spec = ModuleSpec(name="stub", loader=None)
    monkeypatch.setattr(
        "winnow.cli.commands.doctor.importlib.util.find_spec",
        lambda _: spec,
    )
    results = check_optional_extras()
    statuses = {result.status for result in results}
    assert_that(statuses).is_equal_to({CheckStatus.PASS})


def test_check_cache_dir_passes_for_writable_directory(tmp_path: Path) -> None:
    """An existing writable cache directory passes the check."""
    result = check_cache_dir(_config_with_cache_dir(tmp_path))
    assert_that(result.status).is_equal_to(CheckStatus.PASS)
    assert_that(result.detail).contains("writable")


def test_check_cache_dir_warns_for_unwritable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing but unwritable cache directory warns."""
    monkeypatch.setattr(doctor_module, "_directory_is_writable", lambda _: False)
    result = check_cache_dir(_config_with_cache_dir(tmp_path))
    assert_that(result.status).is_equal_to(CheckStatus.WARN)
    assert_that(result.detail).contains("not writable")


def test_check_cache_dir_passes_when_creatable(tmp_path: Path) -> None:
    """A missing cache directory passes when its parent is writable."""
    cache_dir = tmp_path / "nested" / "cache"
    result = check_cache_dir(_config_with_cache_dir(cache_dir))
    assert_that(result.status).is_equal_to(CheckStatus.PASS)
    assert_that(result.detail).contains("can be created")


def test_check_cache_dir_warns_when_uncreatable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing cache directory warns when it cannot be created."""
    monkeypatch.setattr(doctor_module, "_directory_is_writable", lambda _: False)
    cache_dir = tmp_path / "nested" / "cache"
    result = check_cache_dir(_config_with_cache_dir(cache_dir))
    assert_that(result.status).is_equal_to(CheckStatus.WARN)
    assert_that(result.detail).contains("cannot be created")


def test_run_checks_reports_valid_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovered, valid config yields a passing configuration check."""
    config_path = tmp_path / ".winnow-config.yaml"
    monkeypatch.setattr(doctor_module, "find_config_path", lambda: config_path)
    monkeypatch.setattr(doctor_module, "load_config", lambda _: WinnowConfig())
    results = {result.name: result for result in run_checks()}
    assert_that(results["Configuration"].status).is_equal_to(CheckStatus.PASS)
    assert_that(results["Configuration"].detail).contains("valid")


def test_run_checks_reports_invalid_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that fails to load yields a failing configuration check."""
    config_path = tmp_path / ".winnow-config.yaml"

    def _raise(_: Path) -> WinnowConfig:
        raise ConfigError("bad config", operation="load_config")

    monkeypatch.setattr(doctor_module, "find_config_path", lambda: config_path)
    monkeypatch.setattr(doctor_module, "load_config", _raise)
    results = {result.name: result for result in run_checks()}
    assert_that(results["Configuration"].status).is_equal_to(CheckStatus.FAIL)
    assert_that(results["Configuration"].detail).contains("invalid")


def test_run_checks_reports_defaults_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent config falls back to defaults with a passing check."""
    monkeypatch.setattr(doctor_module, "find_config_path", lambda: None)
    results = {result.name: result for result in run_checks()}
    assert_that(results["Configuration"].status).is_equal_to(CheckStatus.PASS)
    assert_that(results["Configuration"].detail).contains("defaults")


def test_doctor_command_exits_zero_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command renders the report and exits zero without hard failures."""
    healthy = [
        CheckResult(name="Python", status=CheckStatus.PASS, detail="ok"),
        CheckResult(name="Extra: face", status=CheckStatus.WARN, detail="optional"),
    ]
    monkeypatch.setattr(doctor_module, "run_checks", lambda: healthy)
    result = CliRunner().invoke(main, ["doctor"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Winnow environment diagnostics")
    assert_that(result.output).contains("PASS")


def test_doctor_command_exits_nonzero_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command exits non-zero when a hard check fails."""
    failing = [
        CheckResult(name="FFmpeg", status=CheckStatus.FAIL, detail="missing"),
    ]
    monkeypatch.setattr(doctor_module, "run_checks", lambda: failing)
    result = CliRunner().invoke(main, ["doctor"])
    assert_that(result.exit_code).is_equal_to(1)
    assert_that(result.output).contains("FAIL")


def test_doctor_command_respects_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command honors the root ``--no-color`` flag."""
    healthy = [CheckResult(name="Python", status=CheckStatus.PASS, detail="ok")]
    monkeypatch.setattr(doctor_module, "run_checks", lambda: healthy)
    result = CliRunner().invoke(main, ["--no-color", "doctor"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).does_not_contain("\x1b[")
