"""Winnow CLI entry point."""

from __future__ import annotations

import click

from winnow import __version__


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="winnow")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Winnow your media library."""
    if ctx.invoked_subcommand is None:
        click.echo(f"winnow {__version__} — use --help for commands.")


if __name__ == "__main__":  # pragma: no cover
    main()
