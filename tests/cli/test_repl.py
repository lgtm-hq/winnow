"""Tests for the interactive REPL and its wiring into the root command."""

from __future__ import annotations

import io
import subprocess  # nosec B404 - fixed interpreter invocation, no untrusted input
import sys

import click
import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow import __version__
from winnow.cli import main
from winnow.cli.repl import run_repl


def test_bare_invocation_shows_tip_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-interactive bare invocation prints the version tip, not the REPL."""
    monkeypatch.setattr("winnow.cli.repl.stdin_is_interactive", lambda: False)
    result = CliRunner().invoke(main, [])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains(f"winnow {__version__}")
    assert_that(result.output).does_not_contain("interactive shell")


def test_bare_invocation_launches_repl_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interactive bare invocation runs the REPL and dispatches commands."""
    monkeypatch.setattr("winnow.cli.repl.stdin_is_interactive", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, [], input="config validate\nexit\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("interactive shell")
    assert_that(result.output).contains("defaults are valid")
    assert_that(result.output).contains("Goodbye.")


def test_repl_reports_unknown_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REPL surfaces unknown commands and stays alive until exit."""
    monkeypatch.setattr("winnow.cli.repl.stdin_is_interactive", lambda: True)
    result = CliRunner().invoke(main, [], input="frobnicate\nquit\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("No such command")
    assert_that(result.output).contains("Goodbye.")


def test_repl_exits_on_end_of_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REPL terminates cleanly when standard input is exhausted."""
    monkeypatch.setattr("winnow.cli.repl.stdin_is_interactive", lambda: True)
    result = CliRunner().invoke(main, [], input="")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Goodbye.")


def test_run_repl_help_command_lists_subcommands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The REPL ``help`` command renders the root command usage."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("help\nexit\n"))
    run_repl(click.Context(main))

    captured = capsys.readouterr()
    assert_that(captured.out).contains("config")
    assert_that(captured.out).contains("init")


def test_cli_package_imports_without_cycles() -> None:
    """Importing the CLI modules in a fresh interpreter never deadlocks."""
    result = subprocess.run(  # nosec B603 - fixed argv with sys.executable, no shell
        [
            sys.executable,
            "-c",
            "import winnow.cli.repl; import winnow.cli.config; "
            "import winnow.cli.init; import winnow.cli",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert_that(result.returncode).is_equal_to(0)
    assert_that(result.stderr).is_empty()
