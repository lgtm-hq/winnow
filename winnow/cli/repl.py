"""Interactive read-eval-print loop launched on a bare ``winnow`` invocation."""

from __future__ import annotations

import shlex
import sys

import click

__all__ = ["run_repl"]

_PROMPT = "winnow> "
_EXIT_COMMANDS = frozenset({"exit", "quit", ":q"})
_HELP_COMMANDS = frozenset({"help", "?"})


def stdin_is_interactive() -> bool:
    """Report whether standard input is an interactive terminal.

    Returns:
        True when ``sys.stdin`` is attached to a TTY.
    """
    stdin = sys.stdin
    return bool(getattr(stdin, "isatty", lambda: False)())


def run_repl(ctx: click.Context) -> None:
    """Run the interactive Winnow shell until end-of-input or an exit command.

    Commands are parsed and dispatched to the root Click group, so every
    subcommand available on the command line is also available here.

    Args:
        ctx: Root command context carrying shared state such as ``no_color``.
    """
    from winnow.cli import main

    context_obj = ctx.ensure_object(dict)
    context_obj["_in_repl"] = True
    click.echo("Winnow interactive shell. Type 'help' for commands, 'exit' to quit.")
    while True:
        line = _read_line()
        if line is None:
            break
        command = line.strip()
        if not command:
            continue
        if command in _EXIT_COMMANDS:
            break
        args = ["--help"] if command in _HELP_COMMANDS else shlex.split(command)
        if not args:
            continue
        _dispatch(main=main, args=args, context_obj=context_obj)
    click.echo("Goodbye.")


def _read_line() -> str | None:
    """Read a single prompted line from standard input.

    Returns:
        The line including no trailing newline, or None at end-of-input.
    """
    click.echo(_PROMPT, nl=False)
    line = sys.stdin.readline()
    if not line:
        click.echo()
        return None
    return line.rstrip("\n")


def _dispatch(
    *,
    main: click.Group,
    args: list[str],
    context_obj: dict[str, object],
) -> None:
    """Dispatch parsed arguments to the root group, keeping the shell alive.

    Args:
        main: Root Click group used to resolve and run commands.
        args: Tokenized command-line arguments.
        context_obj: Shared context object propagated to subcommands.
    """
    try:
        main.main(
            args=args,
            prog_name="winnow",
            standalone_mode=False,
            obj=context_obj,
        )
    except click.ClickException as exc:
        exc.show()
    except click.exceptions.Abort:
        click.echo("Aborted.")
    except SystemExit:
        pass
