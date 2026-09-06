"""Tests for the Winnow CLI console factory and helpers."""

from __future__ import annotations

import importlib
from types import ModuleType

import click
import pytest
from assertpy import assert_that

import winnow.cli.console
from winnow.cli.console import (
    StatusLevel,
    console_from_context,
    create_console,
    format_error,
    print_error,
    status_text,
)


def test_create_console_enables_color_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default console keeps color enabled."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    console = create_console()
    assert_that(console.no_color).is_false()


def test_create_console_disables_color_when_requested() -> None:
    """The ``no_color`` flag suppresses color on the console."""
    console = create_console(no_color=True)
    assert_that(console.no_color).is_true()


def test_create_console_honors_no_color_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``NO_COLOR`` environment variable disables color."""
    monkeypatch.setenv("NO_COLOR", "1")
    console = create_console()
    assert_that(console.no_color).is_true()


def test_create_console_can_target_stderr() -> None:
    """The ``stderr`` flag routes output to standard error."""
    console = create_console(stderr=True)
    assert_that(console.stderr).is_true()


def test_console_from_context_reads_no_color_flag() -> None:
    """The context helper reflects the root ``no_color`` state."""
    ctx = click.Context(click.Command("winnow"))
    ctx.obj = {"no_color": True}
    console = console_from_context(ctx)
    assert_that(console.no_color).is_true()


def test_console_from_context_defaults_to_color_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context without ``no_color`` yields a colored console."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    ctx = click.Context(click.Command("winnow"))
    console = console_from_context(ctx)
    assert_that(console.no_color).is_false()


def test_console_from_context_defaults_to_color_without_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context whose object is ``None`` yields a colored console."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    ctx = click.Context(click.Command("winnow"), obj=None)
    console = console_from_context(ctx)
    assert_that(console.no_color).is_false()


# ``winnow.cli`` re-exports the ``stats``/``info``/``clean`` Click commands under
# the same names as their modules, so resolve the modules explicitly.
_COMMAND_MODULES: list[ModuleType] = [
    importlib.import_module("winnow.cli.stats"),
    importlib.import_module("winnow.cli.info"),
    importlib.import_module("winnow.cli.clean"),
]


@pytest.mark.parametrize(
    "command_module",
    _COMMAND_MODULES,
    ids=["stats", "info", "clean"],
)
def test_command_modules_use_shared_console_factory(
    command_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands bind the single console factory, so ``NO_COLOR`` is honored."""
    monkeypatch.setenv("NO_COLOR", "1")
    factory = command_module.console_from_context
    assert_that(factory).is_same_as(winnow.cli.console.console_from_context)
    ctx = click.Context(click.Command("winnow"))
    assert_that(factory(ctx).no_color).is_true()


def test_status_text_applies_level_style() -> None:
    """Status labels carry the style for their severity level."""
    text = status_text("PASS", StatusLevel.SUCCESS)
    assert_that(str(text)).is_equal_to("PASS")
    assert_that(text.style).is_equal_to("bold green")


def test_format_error_includes_message_and_suggestion() -> None:
    """A formatted error renders the message and suggestion text."""
    console = create_console(no_color=True)
    with console.capture() as capture:
        console.print(format_error("boom", suggestion="try again"))
    output = capture.get()
    assert_that(output).contains("boom")
    assert_that(output).contains("Suggestion:")
    assert_that(output).contains("try again")


def test_format_error_omits_suggestion_when_absent() -> None:
    """An error without a suggestion does not render the suggestion label."""
    console = create_console(no_color=True)
    with console.capture() as capture:
        console.print(format_error("broken"))
    output = capture.get()
    assert_that(output).contains("broken")
    assert_that(output).does_not_contain("Suggestion:")


def test_print_error_writes_panel_to_console() -> None:
    """``print_error`` renders the error panel through the console."""
    console = create_console(no_color=True)
    with console.capture() as capture:
        print_error(console, "kaput", suggestion="reboot")
    output = capture.get()
    assert_that(output).contains("kaput")
    assert_that(output).contains("reboot")
