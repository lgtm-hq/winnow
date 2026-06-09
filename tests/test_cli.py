"""Tests for the Winnow CLI."""

from __future__ import annotations

from assertpy import assert_that
from click.testing import CliRunner

from winnow import __version__
from winnow.cli import main


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


def test_version_option_prints_package_version() -> None:
    """--version prints the installed package version."""
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains(__version__)
