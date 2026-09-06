"""``winnow help`` command rendering Rich-formatted usage.

``winnow help`` prints an overview of the tool and a table of available
commands, while ``winnow help <command>`` prints the detailed usage for a single
command. It complements Click's built-in ``--help`` with a friendlier, styled
landing page and a discoverable command index.
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from winnow.cli.console import console_from_context, print_error
from winnow.cli.errors import ExitCode

__all__ = ["help_command"]


def _render_overview(console: Console, group: click.Group, ctx: click.Context) -> None:
    """Render the tool overview panel and command index table.

    Args:
        console: Console used for rendering.
        group: Root command group whose subcommands are listed.
        ctx: Context used to resolve subcommand metadata.
    """
    summary = group.help or group.short_help or "Winnow your media library."
    console.print(Panel(Text(summary.strip()), title="winnow", title_align="left"))

    table = Table(title="Commands")
    table.add_column("Command", no_wrap=True, style="bold cyan")
    table.add_column("Description", overflow="fold")
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if command is None or command.hidden:
            continue
        table.add_row(name, Text(command.get_short_help_str() or ""))
    console.print(table)
    console.print(
        "Run 'winnow help <command>' or 'winnow <command> --help' for details.",
    )


def _render_command_help(
    console: Console,
    group: click.Group,
    ctx: click.Context,
    command_name: str,
) -> None:
    """Render detailed usage for a single subcommand.

    Args:
        console: Console used for rendering.
        group: Root command group to resolve the command from.
        ctx: Context used as the parent for the command's help context.
        command_name: Name of the command to describe.

    Raises:
        click.exceptions.Exit: With a non-zero code when the command is unknown;
            raised by :meth:`click.Context.exit`.
    """
    command = group.get_command(ctx, command_name)
    if command is None:
        print_error(
            console,
            f"Unknown command: {command_name}",
            suggestion="Run 'winnow help' to list available commands.",
        )
        ctx.exit(ExitCode.USAGE)
        return
    command_ctx = click.Context(command, info_name=command_name, parent=ctx.find_root())
    console.print(
        Panel(
            Text(command.get_help(command_ctx)),
            title=f"winnow {command_name}",
            title_align="left",
        ),
    )


@click.command(name="help")
@click.argument("command_name", required=False, metavar="[COMMAND]")
@click.pass_context
def help_command(ctx: click.Context, command_name: str | None) -> None:
    """Show Rich-formatted help for Winnow or a specific command.

    \f

    Args:
        ctx: Active Click context carrying root option state.
        command_name: Optional command to describe. When omitted, an overview of
            all commands is shown.
    """
    console = console_from_context(ctx)
    root = ctx.find_root()
    group = root.command
    if not isinstance(group, click.Group):  # pragma: no cover - root is always a group
        raise TypeError("help command requires a Click group as the root command")
    if command_name is None:
        _render_overview(console, group, ctx)
        return
    _render_command_help(console, group, ctx, command_name)
