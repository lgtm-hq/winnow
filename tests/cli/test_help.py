"""Tests for the ``winnow help`` command."""

from __future__ import annotations

from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main


def test_help_overview_lists_commands() -> None:
    """``winnow help`` renders an overview and a command index."""
    result = CliRunner().invoke(main, ["help"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("winnow")
    assert_that(result.output).contains("Commands")
    assert_that(result.output).contains("doctor")
    assert_that(result.output).contains("help")


def test_help_for_specific_command_shows_usage() -> None:
    """``winnow help <command>`` renders that command's detailed usage."""
    result = CliRunner().invoke(main, ["help", "doctor"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("winnow doctor")
    assert_that(result.output).contains("Usage:")


def test_help_for_unknown_command_errors() -> None:
    """An unknown command name produces an error and non-zero exit."""
    result = CliRunner().invoke(main, ["help", "nope"])
    assert_that(result.exit_code).is_equal_to(2)
    assert_that(result.output).contains("Unknown command")


def test_help_respects_no_color() -> None:
    """The overview honors the root ``--no-color`` flag."""
    result = CliRunner().invoke(main, ["--no-color", "help"])
    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).does_not_contain("\x1b[")
