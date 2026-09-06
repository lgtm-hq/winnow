"""Winnow CLI entry point."""

from __future__ import annotations

import click

from winnow import __version__
from winnow.cli.cache import cache
from winnow.cli.clean import clean
from winnow.cli.commands import doctor_command, help_command
from winnow.cli.config import config as config_command
from winnow.cli.errors import WinnowGroup
from winnow.cli.info import info
from winnow.cli.init import init as init_command
from winnow.cli.standards import no_color_option
from winnow.cli.stats import stats

__all__ = ["main"]


@click.group(cls=WinnowGroup, invoke_without_command=True)
@click.version_option(version=__version__, prog_name="winnow")
@no_color_option()
@click.pass_context
def main(ctx: click.Context, *, no_color: bool) -> None:
    """Winnow your media library."""
    context_obj = ctx.ensure_object(dict)
    context_obj["no_color"] = no_color
    if ctx.invoked_subcommand is not None:
        return
    from winnow.cli.repl import run_repl, stdin_is_interactive

    if stdin_is_interactive() and not context_obj.get("_in_repl"):
        run_repl(ctx)
    else:
        click.echo(f"winnow {__version__} — use --help for commands.")


main.add_command(cache)
main.add_command(clean)
main.add_command(config_command)
main.add_command(doctor_command)
main.add_command(help_command)
main.add_command(info)
main.add_command(init_command)
main.add_command(stats)


if __name__ == "__main__":  # pragma: no cover
    main()
