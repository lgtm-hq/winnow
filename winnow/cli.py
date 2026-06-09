"""Winnow CLI entry point."""

from __future__ import annotations

import click
from rich.console import Console

from winnow import __version__

console = Console()


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="winnow")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Winnow your media library."""
    if ctx.invoked_subcommand is None:
        console.print(f"winnow {__version__} — use --help for commands.")


if __name__ == "__main__":  # pragma: no cover
    main()
