"""Exit-code table and root error handler for the Winnow CLI.

Every command raises :class:`~winnow.exceptions.WinnowError` subclasses and lets
them propagate; :class:`WinnowGroup` renders them once, on stderr, and maps them
to :attr:`ExitCode.FAILURE`. Ctrl-C maps to :attr:`ExitCode.INTERRUPTED` so
scripts can tell "it failed" from "you stopped it". Click's own usage errors are
left untouched and keep exiting with :attr:`ExitCode.USAGE`.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum
from typing import Any, cast

import click

from winnow.cli.console import console_from_context, print_error
from winnow.exceptions import ConfigError, MediaError, WinnowError

__all__ = ["ExitCode", "WinnowGroup"]


class ExitCode(IntEnum):
    """Process exit codes returned by the ``winnow`` command."""

    SUCCESS = 0
    """The command completed, including "nothing to do" and a decline it handles."""

    FAILURE = 1
    """Any :class:`WinnowError`, a command-reported failure such as ``doctor``,
    or a declined ``click.confirm(..., abort=True)`` prompt (Click's ``Abort``)."""

    USAGE = 2
    """A Click usage error: bad flag, missing argument, or unknown command."""

    INTERRUPTED = 130
    """Interrupted by Ctrl-C (128 + SIGINT)."""


_CONFIG_VALIDATE_PATH = "config validate"
"""Command path of the validator itself; it must never suggest running itself."""


def _config_suggestion(exc: ConfigError, command_path: str | None) -> str:
    """Pick the hint for a configuration error.

    Args:
        exc: The configuration error being reported.
        command_path: Space-joined names of the invoked command, when known.

    Returns:
        A suggestion that never tells ``config validate`` to run itself.
    """
    if exc.context.operation == "validate_config":
        return "Fix the keys listed above, or run 'winnow init' to regenerate the file."
    if command_path == _CONFIG_VALIDATE_PATH:
        return "Fix the file, or run 'winnow init' to regenerate it."
    return "Run 'winnow config validate' or 'winnow init'."


def _suggestion_for(exc: WinnowError, *, command_path: str | None) -> str | None:
    """Pick an actionable hint for a domain error, when one exists.

    Args:
        exc: The error being reported.
        command_path: Space-joined names of the invoked command, when known.

    Returns:
        A suggestion line for the error panel, or ``None`` when there is none.
    """
    if isinstance(exc, ConfigError):
        return _config_suggestion(exc, command_path=command_path)
    if isinstance(exc, MediaError) and exc.context.file_path is not None:
        return f"Check that {exc.context.file_path} is a readable media file."
    return None


def _is_field_error_list(value: object) -> bool:
    """Report whether a value looks like Pydantic's ``ValidationError.errors()``.

    Args:
        value: A ``details`` entry.

    Returns:
        True when ``value`` is a non-empty list of mappings carrying ``loc`` and
        ``msg``.
    """
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, Mapping) and "loc" in item and "msg" in item
            for item in value
        )
    )


def _detail_lines(details: Mapping[str, object]) -> list[str]:
    """Render structured error details as indented lines.

    Args:
        details: The ``ErrorContext.details`` mapping.

    Returns:
        One line per Pydantic field error (``loc.path: msg``) under ``errors``,
        and one ``key: value`` line for every other entry.
    """
    lines: list[str] = []
    for key, value in details.items():
        if key == "errors" and _is_field_error_list(value):
            lines.extend(
                f"  {'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in cast("list[Mapping[str, Any]]", value)
            )
        else:
            text = str(value).replace("\n", "\n  ")
            lines.append(f"  {key}: {text}")
    return lines


def _describe(exc: WinnowError) -> str:
    """Format a domain error message with its structured context.

    Args:
        exc: The error being reported.

    Returns:
        The message followed by ``(operation: ..., path: ...)`` for whichever
        context fields are present, then one indented line per detail.
    """
    parts: list[str] = []
    if exc.context.operation is not None:
        parts.append(f"operation: {exc.context.operation}")
    if exc.context.file_path is not None:
        parts.append(f"path: {exc.context.file_path}")
    head = exc.message if not parts else f"{exc.message} ({', '.join(parts)})"
    return "\n".join([head, *_detail_lines(exc.context.details)])


def _invoked_command_path(
    group: click.Group,
    ctx: click.Context,
    args: list[str],
) -> str | None:
    """Resolve the space-joined path of the subcommand the root group dispatched.

    Click discards the leaf context before an exception reaches the root
    handler, so the path is rebuilt from the subcommand name recorded on the
    root context and the argument tokens captured before dispatch.

    Args:
        group: The root command group.
        ctx: Root context after dispatch.
        args: Root ``ctx.args`` as they were before dispatch.

    Returns:
        For example ``"config validate"``, or ``None`` when no subcommand ran.
    """
    if ctx.invoked_subcommand is None:
        return None
    names = [ctx.invoked_subcommand]
    command = group.get_command(ctx, ctx.invoked_subcommand)
    for token in args:
        if not isinstance(command, click.Group):
            break
        child = command.get_command(ctx, token)
        if child is None:
            break
        names.append(token)
        command = child
    return " ".join(names)


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
        args = list(ctx.args)
        try:
            return super().invoke(ctx)
        except WinnowError as exc:
            console = console_from_context(ctx, stderr=True)
            command_path = _invoked_command_path(group=self, ctx=ctx, args=args)
            print_error(
                console,
                _describe(exc),
                suggestion=_suggestion_for(exc, command_path=command_path),
            )
            ctx.exit(ExitCode.FAILURE)
        except KeyboardInterrupt:
            console = console_from_context(ctx, stderr=True)
            console.print("Interrupted.")
            ctx.exit(ExitCode.INTERRUPTED)
