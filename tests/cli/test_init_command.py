"""Tests for the ``winnow init`` command."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.config import CONFIG_FILE_NAME, load_config
from winnow.exceptions import ConfigError
from winnow.models.enums import HashAlgorithm, SortOrder

_ACCEPT_DEFAULTS = "\nphash\nby_size\n4\nn\n"


def test_init_creates_config_from_prompts(tmp_path: Path) -> None:
    """``init`` writes a validated config reflecting the guided answers."""
    config_path = tmp_path / CONFIG_FILE_NAME

    result = CliRunner().invoke(
        main,
        ["init", "--config", str(config_path)],
        input="/data/media" + _ACCEPT_DEFAULTS,
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(config_path.is_file()).is_true()
    config_model = load_config(config_path=config_path)
    assert_that(config_model.hash_algorithm).is_equal_to(HashAlgorithm.PHASH)
    assert_that(config_model.sort_order).is_equal_to(SortOrder.BY_SIZE)
    assert_that(config_model.workers).is_equal_to(4)
    assert_that(config_model.dry_run).is_false()
    assert_that(config_model.source_dirs).is_equal_to([Path("/data/media")])


def test_init_skips_blank_source_directory(tmp_path: Path) -> None:
    """``init`` leaves source_dirs empty when the source prompt is blank."""
    config_path = tmp_path / CONFIG_FILE_NAME

    result = CliRunner().invoke(
        main,
        ["init", "--config", str(config_path)],
        input=_ACCEPT_DEFAULTS,
    )

    assert_that(result.exit_code).is_equal_to(0)
    config_model = load_config(config_path=config_path)
    assert_that(config_model.source_dirs).is_equal_to([])


def test_init_aborts_when_overwrite_declined(tmp_path: Path) -> None:
    """``init`` preserves an existing config when overwrite is declined."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 7\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["init", "--config", str(config_path)],
        input="n\n",
    )

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(config_path.read_text(encoding="utf-8")).contains("workers: 7")


def test_init_yes_overwrites_existing_config(tmp_path: Path) -> None:
    """``init --yes`` overwrites an existing config without prompting."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 7\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["init", "--yes", "--config", str(config_path)],
        input="/data/media" + _ACCEPT_DEFAULTS,
    )

    assert_that(result.exit_code).is_equal_to(0)
    config_model = load_config(config_path=config_path)
    assert_that(config_model.workers).is_equal_to(4)


def test_init_reports_config_error_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init`` lets a ``ConfigError`` reach the root handler's stderr panel."""
    config_path = tmp_path / CONFIG_FILE_NAME

    def fail(*, config_path: Path, overwrite: bool) -> Path:
        raise ConfigError(
            "disk full",
            operation="generate_config",
            file_path=config_path,
        )

    monkeypatch.setattr("winnow.cli.init.generate_default_config", fail)

    result = CliRunner().invoke(
        main,
        ["init", "--config", str(config_path)],
        input=_ACCEPT_DEFAULTS,
    )

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("disk full")
    assert_that(result.stderr).contains("operation: generate_config")
    assert_that(result.stderr).contains("winnow init")
    assert_that(result.stdout).does_not_contain("Created configuration")
    assert_that(config_path.exists()).is_false()
