"""``winnow clean`` command for pruning empty directories.

Discovery and removal live in :mod:`winnow.fs.empty_dirs`; this module only
parses options, previews candidates, confirms, and renders the outcome. The
target root is always preserved.
"""

from __future__ import annotations

from pathlib import Path

import click

from winnow.cli.console import console_from_context
from winnow.cli.standards import dry_run_option, yes_option
from winnow.fs.empty_dirs import find_empty_directories, remove_empty_tree
from winnow.fs.errors import FileSystemOperationError

__all__ = ["clean"]


@click.command()
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--exclude",
    "exclude_patterns",
    multiple=True,
    metavar="PATTERN",
    help="Glob pattern for directories to preserve; repeatable.",
)
@dry_run_option()
@yes_option()
@click.pass_context
def clean(
    ctx: click.Context,
    *,
    directory: Path,
    exclude_patterns: tuple[str, ...],
    dry_run: bool,
    yes: bool,
) -> None:
    """Remove empty directories under DIRECTORY.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        directory: Root directory to prune.
        exclude_patterns: Glob patterns for directories to preserve.
        dry_run: When set, report candidates without deleting anything.
        yes: When set, skip the interactive confirmation prompt.

    Raises:
        click.ClickException: If a candidate directory cannot be removed.
    """
    console = console_from_context(ctx)
    candidates = find_empty_directories(directory, exclude_patterns=exclude_patterns)

    if not candidates:
        console.print("No empty directories found.")
        return

    if dry_run:
        for candidate in candidates:
            console.print(f"Would remove: {candidate}")
        console.print(
            f"{len(candidates)} empty director"
            f"{'y' if len(candidates) == 1 else 'ies'} to remove (dry run).",
        )
        return

    if not yes and not click.confirm(
        f"Remove {len(candidates)} empty director"
        f"{'y' if len(candidates) == 1 else 'ies'}?",
    ):
        console.print("Aborted.")
        return

    try:
        removed = remove_empty_tree(
            directory,
            exclude_patterns=exclude_patterns,
            include_root=False,
        )
    except FileSystemOperationError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(
        f"Removed {len(removed)} empty director{'y' if len(removed) == 1 else 'ies'}.",
    )
