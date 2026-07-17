"""Winnow CLI entry point."""

from __future__ import annotations

import click

from winnow import __version__
from winnow.cli.standards import no_color_option

__all__ = ["main"]


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="winnow")
@no_color_option()
@click.pass_context
def main(ctx: click.Context, no_color: bool) -> None:
    """Winnow your media library."""
    context_obj = ctx.ensure_object(dict)
    context_obj["no_color"] = no_color
    if ctx.invoked_subcommand is None:
        click.echo(f"winnow {__version__} — use --help for commands.")


if __name__ == "__main__":  # pragma: no cover
    main()
