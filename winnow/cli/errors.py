"""Exit-code table and root error handler for the Winnow CLI.

Every command raises :class:`~winnow.exceptions.WinnowError` subclasses and lets
them propagate; :class:`WinnowGroup` renders them once, on stderr, and maps them
to :attr:`ExitCode.FAILURE`. Ctrl-C maps to :attr:`ExitCode.INTERRUPTED` so
scripts can tell "it failed" from "you stopped it". Click's own usage errors are
left untouched and keep exiting with :attr:`ExitCode.USAGE`.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

import click

from winnow.cli.console import console_from_context, print_error
from winnow.exceptions import ConfigError, MediaError, WinnowError

__all__ = ["ExitCode", "WinnowGroup"]


class ExitCode(IntEnum):
    """Process exit codes returned by the ``winnow`` command."""

    SUCCESS = 0
    """The command completed, including "nothing to do" and declined prompts."""

    FAILURE = 1
    """Any :class:`WinnowError` or a command-reported failure such as ``doctor``."""

    USAGE = 2
    """A Click usage error: bad flag, missing argument, or unknown command."""

    INTERRUPTED = 130
    """Interrupted by Ctrl-C (128 + SIGINT)."""


def _suggestion_for(exc: WinnowError) -> str | None:
    """Pick an actionable hint for a domain error, when one exists.

    Args:
        exc: The error being reported.

    Returns:
        A suggestion line for the error panel, or ``None`` when there is none.
    """
    if isinstance(exc, ConfigError):
        return "Run 'winnow config validate' or 'winnow init'."
    if isinstance(exc, MediaError) and exc.context.file_path is not None:
        return f"Check that {exc.context.file_path} is a readable media file."
    return None


def _describe(exc: WinnowError) -> str:
    """Format a domain error message with its structured context.

    Args:
        exc: The error being reported.

    Returns:
        The message followed by ``(operation: ..., path: ...)`` for whichever
        context fields are present.
    """
    parts: list[str] = []
    if exc.context.operation is not None:
        parts.append(f"operation: {exc.context.operation}")
    if exc.context.file_path is not None:
        parts.append(f"path: {exc.context.file_path}")
    if not parts:
        return exc.message
    return f"{exc.message} ({', '.join(parts)})"


class WinnowGroup(click.Group):
    """Root command group that applies the :class:`ExitCode` table.

    ``invoke`` runs after Click has parsed arguments, so usage errors are still
    Click's own, and inside ``main()``'s ``standalone_mode`` handling, so the
    :class:`click.exceptions.Exit` raised here is honoured both on the command
    line and in the REPL.
    """

    def invoke(self, ctx: click.Context) -> Any:
        """Run the resolved command and translate domain errors to exit codes.

        Args:
            ctx: Root context for this invocation.

        Returns:
            Whatever the invoked command returns.

        Raises:
            click.exceptions.Exit: With :attr:`ExitCode.FAILURE` for a
                :class:`WinnowError` and :attr:`ExitCode.INTERRUPTED` for a
                :class:`KeyboardInterrupt`; raised by :meth:`click.Context.exit`.
        """
        try:
            return super().invoke(ctx)
        except WinnowError as exc:
            console = console_from_context(ctx, stderr=True)
            print_error(console, _describe(exc), suggestion=_suggestion_for(exc))
            ctx.exit(ExitCode.FAILURE)
        except KeyboardInterrupt:
            console = console_from_context(ctx, stderr=True)
            console.print("Interrupted.")
            ctx.exit(ExitCode.INTERRUPTED)
