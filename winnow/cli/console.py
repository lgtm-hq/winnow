"""Rich console factory and shared output helpers for the Winnow CLI.

This module centralizes how Winnow renders terminal output so every command
shares one voice. Commands should obtain a :class:`rich.console.Console` via
:func:`create_console` (or :func:`console_from_context` inside a Click command)
rather than calling :func:`print`, and should format user-facing errors through
:func:`format_error` so suggestions render consistently.

Output standards:

- Colors and styling are enabled by default but suppressed when the root CLI is
  invoked with ``--no-color`` or when the ``NO_COLOR`` environment variable is
  set, honoring the https://no-color.org/ convention.
- Errors are shown in a red panel and may carry an actionable suggestion.
- Status labels use a small, fixed vocabulary (see :class:`StatusLevel`).
"""

from __future__ import annotations

import os
from enum import StrEnum, auto

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

__all__ = [
    "StatusLevel",
    "console_from_context",
    "create_console",
    "format_error",
    "print_error",
    "status_text",
]


class StatusLevel(StrEnum):
    """Severity level for a single line of status output."""

    SUCCESS = auto()
    WARNING = auto()
    ERROR = auto()
    INFO = auto()


_STATUS_STYLES: dict[StatusLevel, str] = {
    StatusLevel.SUCCESS: "bold green",
    StatusLevel.WARNING: "bold yellow",
    StatusLevel.ERROR: "bold red",
    StatusLevel.INFO: "bold cyan",
}


def _color_disabled(*, no_color: bool) -> bool:
    """Determine whether colored output should be suppressed.

    Args:
        no_color: Whether the caller explicitly requested no color, typically
            from the root ``--no-color`` flag.

    Returns:
        ``True`` when color must be disabled, honoring the ``NO_COLOR``
        environment variable convention in addition to the explicit flag.
    """
    return no_color or bool(os.environ.get("NO_COLOR"))


def create_console(*, no_color: bool = False, stderr: bool = False) -> Console:
    """Create a Rich console configured for Winnow output.

    Args:
        no_color: Whether to disable colored and styled output.
        stderr: Whether the console should write to standard error instead of
            standard output.

    Returns:
        A configured :class:`rich.console.Console`.
    """
    disable_color = _color_disabled(no_color=no_color)
    return Console(
        stderr=stderr,
        no_color=disable_color,
        color_system=None if disable_color else "auto",
        highlight=False,
        markup=False,
        emoji=False,
    )


def console_from_context(ctx: click.Context, *, stderr: bool = False) -> Console:
    """Build a console honoring the root ``--no-color`` flag from the context.

    Args:
        ctx: Active Click context whose object holds root option state.
        stderr: Whether the console should write to standard error.

    Returns:
        A console whose color setting reflects ``ctx.obj["no_color"]``.
    """
    context_obj = ctx.ensure_object(dict)
    no_color = bool(context_obj.get("no_color", False))
    return create_console(no_color=no_color, stderr=stderr)


def status_text(label: str, level: StatusLevel) -> Text:
    """Render a status label styled for its severity.

    Args:
        label: Text to display, such as ``"PASS"`` or ``"FAIL"``.
        level: Severity level controlling the applied style.

    Returns:
        A styled :class:`rich.text.Text` for the label.
    """
    return Text(label, style=_STATUS_STYLES[level])


def format_error(message: str, *, suggestion: str | None = None) -> Panel:
    """Build a Rich panel that presents an error and optional suggestion.

    Args:
        message: Human-readable error description.
        suggestion: Optional actionable hint shown beneath the message.

    Returns:
        A red-bordered panel containing the error and any suggestion.
    """
    body = Text(message, style="red")
    if suggestion is not None:
        body.append("\n\n")
        body.append("Suggestion: ", style="bold")
        body.append(suggestion)
    return Panel(body, title="Error", border_style="red", title_align="left")


def print_error(
    console: Console,
    message: str,
    *,
    suggestion: str | None = None,
) -> None:
    """Print a formatted error panel to a console.

    Args:
        console: Console used to render the error.
        message: Human-readable error description.
        suggestion: Optional actionable hint shown beneath the message.
    """
    console.print(format_error(message, suggestion=suggestion))
