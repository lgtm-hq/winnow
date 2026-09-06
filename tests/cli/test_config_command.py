"""Tests for the ``winnow config`` command group."""

from __future__ import annotations

import json
from pathlib import Path

from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.config import CONFIG_FILE_NAME, load_config


def test_config_show_renders_yaml_defaults() -> None:
    """``config show`` renders default configuration as YAML."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["config", "show"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("hash_algorithm: sha256")
    assert_that(result.output).contains("workers: 1")


def test_config_show_json_uses_explicit_config(tmp_path: Path) -> None:
    """``config show --format json`` reflects an explicit config file."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 6\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["config", "show", "--format", "json", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    payload = json.loads(result.output)
    assert_that(payload["workers"]).is_equal_to(6)


def test_config_set_persists_typed_value(tmp_path: Path) -> None:
    """``config set`` writes a JSON-parsed value that survives reloading."""
    config_path = tmp_path / CONFIG_FILE_NAME

    result = CliRunner().invoke(
        main,
        ["config", "set", "cache.enabled", "false", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(config_path.is_file()).is_true()
    reloaded = load_config(config_path=config_path)
    assert_that(reloaded.cache.enabled).is_false()


def test_config_set_rejects_unknown_key(tmp_path: Path) -> None:
    """``config set`` surfaces validation errors as command failures."""
    config_path = tmp_path / CONFIG_FILE_NAME

    result = CliRunner().invoke(
        main,
        ["config", "set", "bogus", "1", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("Invalid Winnow configuration")
    assert_that(result.stderr).contains("winnow config validate")
    assert_that(result.stdout).is_empty()


def test_config_reset_requires_confirmation(tmp_path: Path) -> None:
    """``config reset`` aborts when the confirmation prompt is declined."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 9\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["config", "reset", "--config", str(config_path)],
        input="n\n",
    )

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(config_path.read_text(encoding="utf-8")).contains("workers: 9")


def test_config_reset_writes_defaults_with_yes(tmp_path: Path) -> None:
    """``config reset --yes`` overwrites the file with defaults."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 9\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["config", "reset", "--yes", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    reloaded = load_config(config_path=config_path)
    assert_that(reloaded.workers).is_equal_to(1)


def test_config_validate_reports_valid_file(tmp_path: Path) -> None:
    """``config validate`` confirms a well-formed configuration file."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 3\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["config", "validate", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("is valid")


def test_config_validate_reports_invalid_file(tmp_path: Path) -> None:
    """``config validate`` fails on an out-of-range value."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 0\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["config", "validate", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("Invalid Winnow configuration")
    assert_that(result.stderr).contains("operation: validate_config")
    assert_that(result.stderr).contains("path:")
    assert_that(result.stdout).is_empty()
