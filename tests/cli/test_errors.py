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
    assert_that(result.stderr).contains("winnow init")
    assert_that(result.stderr).does_not_contain("winnow config validate")
    assert_that(result.stdout).is_empty()


def _invoke_raising(group: WinnowGroup, exc: ConfigError) -> str:
    """Run a throwaway command that raises ``exc`` and return the stderr panel.

    Args:
        group: Root group to register the command on.
        exc: Error the command raises.

    Returns:
        Everything the root handler printed on stderr.
    """

    @group.command(name="raise-it")
    def raise_it() -> None:
        """Raise the prepared error."""
        raise exc

    result = CliRunner().invoke(group, ["raise-it"])
    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    return result.stderr


def test_panel_renders_pydantic_field_errors(group: WinnowGroup) -> None:
    """Pydantic ``errors()`` details render as one ``loc: msg`` line each."""
    exc = ConfigError(
        "Invalid Winnow configuration",
        operation="validate_config",
        file_path="/x",
        details={
            "errors": [
                {
                    "loc": ("cache", "enabled"),
                    "msg": "Input should be a valid boolean",
                    "type": "bool_parsing",
                },
                {"loc": ("workers",), "msg": "Input should be greater than 0"},
            ],
        },
    )

    stderr = _invoke_raising(group, exc)

    assert_that(stderr).contains("operation: validate_config")
    assert_that(stderr).contains("cache.enabled: Input should be a valid boolean")
    assert_that(stderr).contains("workers: Input should be greater than 0")
    assert_that(stderr).does_not_contain("bool_parsing")


def test_panel_renders_plain_details(group: WinnowGroup) -> None:
    """Non-Pydantic details render as ``key: value`` lines."""
    exc = ConfigError(
        "Unable to load Winnow configuration",
        operation="load_config",
        details={"error": "mapping values are not allowed here"},
    )

    stderr = _invoke_raising(group, exc)

    assert_that(stderr).contains("error: mapping values are not allowed here")


def test_panel_without_details_has_no_detail_lines(group: WinnowGroup) -> None:
    """An error with no details keeps the single-line rendering."""
    exc = ConfigError("bad", operation="load_config", file_path="/x")

    stderr = _invoke_raising(group, exc)

    assert_that(stderr).contains("bad (operation: load_config, path: /x)")
    assert_that(stderr).does_not_contain("error:")


def test_validate_config_error_hint_does_not_name_config_validate(
    group: WinnowGroup,
) -> None:
    """A ``validate_config`` error points at the listed keys, not the validator."""

    @group.command(name="validate-error")
    def validate_error() -> None:
        """Raise a ``ConfigError`` from the validation step."""
        raise ConfigError("bad", operation="validate_config")

    result = CliRunner().invoke(group, ["validate-error"])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("Fix the keys listed above")
    assert_that(result.stderr).does_not_contain("winnow config validate")
