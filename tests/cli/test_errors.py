"""Tests for the CLI exit-code table and the ``WinnowGroup`` root handler."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.errors import ExitCode, WinnowGroup
from winnow.exceptions import ConfigError, HashError, MediaError


@pytest.fixture
def group() -> WinnowGroup:
    """Build a throwaway root group with one command per failure mode.

    Returns:
        A ``WinnowGroup`` carrying ``ok``, ``needs-arg``, ``config-error``,
        ``media-error``, ``bare-error``, and ``interrupt`` commands.
    """
    root = WinnowGroup(name="root")

    @root.command(name="ok")
    def ok() -> None:
        """Return normally."""
        click.echo("fine")

    @root.command(name="needs-arg")
    @click.argument("value")
    def needs_arg(value: str) -> None:
        """Require one positional argument."""
        click.echo(value)

    @root.command(name="config-error")
    def config_error() -> None:
        """Raise a ``ConfigError`` with full context."""
        raise ConfigError("bad", operation="load_config", file_path="/x")

    @root.command(name="media-error")
    def media_error() -> None:
        """Raise a ``MediaError`` naming a file."""
        raise MediaError("nope", file_path="/f.jpg")

    @root.command(name="bare-error")
    def bare_error() -> None:
        """Raise a ``WinnowError`` subclass without context."""
        raise HashError("digest failed")

    @root.command(name="interrupt")
    def interrupt() -> None:
        """Simulate Ctrl-C."""
        raise KeyboardInterrupt

    return root


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (ExitCode.SUCCESS, 0),
        (ExitCode.FAILURE, 1),
        (ExitCode.USAGE, 2),
        (ExitCode.INTERRUPTED, 130),
    ],
    ids=["success", "failure", "usage", "interrupted"],
)
def test_exit_code_table(member: ExitCode, value: int) -> None:
    """The exit-code table matches the documented process contract."""
    assert_that(int(member)).is_equal_to(value)


def test_main_is_a_winnow_group() -> None:
    """The root ``winnow`` command is built on ``WinnowGroup``."""
    assert_that(main).is_instance_of(WinnowGroup)


def test_normal_return_exits_zero(group: WinnowGroup) -> None:
    """A command that returns normally exits 0 with its stdout intact."""
    result = CliRunner().invoke(group, ["ok"])

    assert_that(result.exit_code).is_equal_to(ExitCode.SUCCESS)
    assert_that(result.stdout).contains("fine")
    assert_that(result.stderr).is_empty()


def test_missing_argument_is_a_click_usage_error(group: WinnowGroup) -> None:
    """A missing required argument keeps Click's usage error and exit 2."""
    result = CliRunner().invoke(group, ["needs-arg"])

    assert_that(result.exit_code).is_equal_to(ExitCode.USAGE)
    assert_that(result.stderr).contains("Missing argument")
    assert_that(result.stdout).is_empty()


def test_config_error_renders_context_and_suggestion(group: WinnowGroup) -> None:
    """A ``ConfigError`` exits 1 with its context and hint on stderr only."""
    result = CliRunner().invoke(group, ["config-error"])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("bad")
    assert_that(result.stderr).contains("operation: load_config")
    assert_that(result.stderr).contains("path: /x")
    assert_that(result.stderr).contains("winnow config validate")
    assert_that(result.stdout).is_empty()


def test_media_error_suggestion_names_the_file(group: WinnowGroup) -> None:
    """A ``MediaError`` with a path exits 1 and its hint names that path."""
    result = CliRunner().invoke(group, ["media-error"])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("nope")
    assert_that(result.stderr).contains("Check that /f.jpg is a readable media file.")
    assert_that(result.stdout).is_empty()


def test_error_without_context_has_no_suggestion(group: WinnowGroup) -> None:
    """A context-free ``WinnowError`` renders the bare message and exits 1."""
    result = CliRunner().invoke(group, ["bare-error"])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("digest failed")
    assert_that(result.stderr).does_not_contain("operation:")
    assert_that(result.stderr).does_not_contain("Suggestion:")
    assert_that(result.stdout).is_empty()


def test_keyboard_interrupt_exits_130(group: WinnowGroup) -> None:
    """Ctrl-C prints ``Interrupted.`` on stderr and exits 130."""
    result = CliRunner().invoke(group, ["interrupt"])

    assert_that(result.exit_code).is_equal_to(ExitCode.INTERRUPTED)
    assert_that(result.stderr).contains("Interrupted.")
    assert_that(result.stderr).does_not_contain("Aborted")
    assert_that(result.stdout).is_empty()


def test_config_validate_missing_file_through_main(tmp_path: Path) -> None:
    """``winnow config validate`` on a missing file exits 1 with a stderr panel."""
    missing = tmp_path / "missing.yaml"

    result = CliRunner().invoke(main, ["config", "validate", "--config", str(missing)])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("Error")
    assert_that(result.stderr).contains("operation: load_config")
    assert_that(result.stderr).contains("winnow config validate")
    assert_that(result.stdout).is_empty()
