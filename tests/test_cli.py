"""Tests for the Winnow CLI."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import click
import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow import __version__
from winnow.cli import main
from winnow.cli.standards import (
    OutputFormat,
    cache_options,
    config_path_option,
    dry_run_option,
    format_option,
    no_color_option,
    output_option,
    processing_command_options,
    reporting_command_options,
    standard_command_options,
    workers_option,
    yes_option,
)

CommandCallback = Callable[..., None]
CommandDecorator = Callable[[CommandCallback], CommandCallback]


def _render_value(value: object) -> str:
    """Render Click callback values for deterministic assertions.

    Args:
        value: Callback value produced by Click.

    Returns:
        A stable string representation for test output.
    """
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)


def _command_with_options(decorators: Sequence[CommandDecorator]) -> click.Command:
    """Build a temporary Click command with shared option decorators.

    Args:
        decorators: Option decorators to apply to the command callback.

    Returns:
        A Click command suitable for isolated decorator tests.
    """

    def callback(**kwargs: object) -> None:
        """Echo parsed option values from a temporary command."""
        for key, value in sorted(kwargs.items()):
            click.echo(f"{key}={_render_value(value)}")

    decorated_callback: CommandCallback = callback
    for decorator in decorators:
        decorated_callback = decorator(decorated_callback)
    return click.command()(decorated_callback)


def test_main_prints_version_hint_when_invoked_without_subcommand() -> None:
    """Root command prints version guidance when no subcommand is given."""
    runner = CliRunner()
    result = runner.invoke(main, [])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains(f"winnow {__version__}")


def test_help_option_shows_usage() -> None:
    """--help prints the command group usage without invoking the default handler."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Winnow your media library")
    assert_that(result.output).contains("--no-color")


def test_version_option_prints_package_version() -> None:
    """--version prints the installed package version."""
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains(__version__)


def test_main_mutates_existing_context_object_for_root_options() -> None:
    """Root option state is added to an existing Click context object."""
    context_obj: dict[str, object] = {"existing": "kept"}
    result = CliRunner().invoke(main, ["--no-color"], obj=context_obj)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(context_obj).is_equal_to({"existing": "kept", "no_color": True})


def test_standard_option_factories_expose_conventional_flags() -> None:
    """Shared option factories expose the canonical CLI flag vocabulary."""
    command = _command_with_options(
        [
            dry_run_option(),
            yes_option(),
            no_color_option(),
            format_option(),
            output_option(),
            workers_option(),
            config_path_option(),
            cache_options(),
        ],
    )
    result = CliRunner().invoke(command, ["--help"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("--dry-run")
    assert_that(result.output).contains("--yes")
    assert_that(result.output).contains("-y")
    assert_that(result.output).contains("--no-color")
    assert_that(result.output).contains("--format")
    assert_that(result.output).contains("-f")
    assert_that(result.output).contains("json")
    assert_that(result.output).contains("csv")
    assert_that(result.output).contains("table")
    assert_that(result.output).contains("markdown")
    assert_that(result.output).contains("--output")
    assert_that(result.output).contains("-o")
    assert_that(result.output).contains("--workers")
    assert_that(result.output).contains("-w")
    assert_that(result.output).contains("--config")
    assert_that(result.output).contains("--enable-cache")
    assert_that(result.output).contains("--no-cache")
    assert_that(result.output).contains("--cache-path")
    assert_that(result.output).contains("--cache-ttl")
    assert_that(result.output).does_not_contain("--force")
    assert_that(result.output).does_not_contain("--disable-cache")


def test_standard_option_factories_parse_canonical_parameter_names() -> None:
    """Shared options parse into stable callback parameter names."""
    command = _command_with_options(
        [
            dry_run_option(),
            yes_option(),
            no_color_option(),
            format_option(),
            output_option(),
            workers_option(),
            config_path_option(),
            cache_options(),
        ],
    )
    result = CliRunner().invoke(
        command,
        [
            "--dry-run",
            "-y",
            "--no-color",
            "-f",
            "json",
            "-o",
            "report.json",
            "-w",
            "8",
            "--config",
            "winnow.yml",
            "--no-cache",
            "--cache-path",
            ".winnow-cache",
            "--cache-ttl",
            "60",
        ],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("cache_path=.winnow-cache")
    assert_that(result.output).contains("cache_ttl=60")
    assert_that(result.output).contains("config_path=winnow.yml")
    assert_that(result.output).contains("dry_run=True")
    assert_that(result.output).contains("enable_cache=False")
    assert_that(result.output).contains("format=json")
    assert_that(result.output).contains("no_color=True")
    assert_that(result.output).contains("output=report.json")
    assert_that(result.output).contains("workers=8")
    assert_that(result.output).contains("yes=True")


def test_standard_command_options_exclude_root_no_color_flag() -> None:
    """Shared subcommand options leave --no-color on the root command."""
    command = _command_with_options([standard_command_options])
    result = CliRunner().invoke(command, ["--help"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("--config")
    assert_that(result.output).contains("--dry-run")
    assert_that(result.output).contains("--yes")
    assert_that(result.output).does_not_contain("--no-color")


def test_reporting_command_options_apply_standard_and_report_flags() -> None:
    """Reporting composite applies subcommand, format, and output flags."""
    command = _command_with_options([reporting_command_options])
    result = CliRunner().invoke(command, ["--help"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("--config")
    assert_that(result.output).contains("--dry-run")
    assert_that(result.output).contains("--yes")
    assert_that(result.output).does_not_contain("--no-color")
    assert_that(result.output).contains("--format")
    assert_that(result.output).contains("--output")


def test_processing_command_options_apply_standard_worker_and_cache_flags() -> None:
    """Processing composite applies subcommand, worker, and cache flags."""
    command = _command_with_options([processing_command_options])
    result = CliRunner().invoke(command, ["--help"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("--config")
    assert_that(result.output).contains("--dry-run")
    assert_that(result.output).contains("--yes")
    assert_that(result.output).does_not_contain("--no-color")
    assert_that(result.output).contains("--workers")
    assert_that(result.output).contains("--enable-cache")
    assert_that(result.output).contains("--no-cache")


def test_workers_option_rejects_zero_workers() -> None:
    """Worker count must be a positive integer."""
    command = _command_with_options([workers_option()])
    result = CliRunner().invoke(command, ["--workers", "0"])

    assert_that(result.exit_code).is_not_equal_to(0)
    assert_that(result.output).contains("Invalid value for '--workers'")
    assert_that(result.output).contains("x>=1")


def test_format_option_accepts_custom_default() -> None:
    """A custom default format appears in help and parses as the default."""

    @click.command()
    @format_option(default=OutputFormat.JSON)
    def report(**kwargs: object) -> None:
        """Echo parsed parameters for assertions."""
        click.echo(f"format={kwargs['format']}")

    runner = CliRunner()
    help_result = runner.invoke(report, ["--help"])
    parse_result = runner.invoke(report, [])

    assert_that(help_result.exit_code).is_equal_to(0)
    assert_that(help_result.output).contains("[default: json]")
    assert_that(parse_result.exit_code).is_equal_to(0)
    assert_that(parse_result.output).contains("format=json")


def test_format_option_rejects_unknown_default() -> None:
    """An unsupported default format fails fast at decorator creation."""
    with pytest.raises(ValueError):
        format_option(default="xml")


def test_workers_option_accepts_custom_default() -> None:
    """A custom worker default appears in help and parses as the default."""

    @click.command()
    @workers_option(default=8)
    def process(**kwargs: object) -> None:
        """Echo parsed parameters for assertions."""
        click.echo(f"workers={kwargs['workers']}")

    runner = CliRunner()
    help_result = runner.invoke(process, ["--help"])
    parse_result = runner.invoke(process, [])

    assert_that(help_result.output).contains("default: 8")
    assert_that(parse_result.output).contains("workers=8")


def test_cache_ttl_rejects_negative_values() -> None:
    """A negative cache TTL fails at parse time."""

    @click.command()
    @cache_options()
    def cached(**kwargs: object) -> None:
        """Accept cache options for assertions."""
        del kwargs

    runner = CliRunner()
    result = runner.invoke(cached, ["--cache-ttl", "-5"])

    assert_that(result.exit_code).is_not_equal_to(0)
