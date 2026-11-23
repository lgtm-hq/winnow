"""``winnow clean`` command for pruning empty directories.

Discovery and removal live in :mod:`winnow.fs.empty_dirs`; this module only
parses options, previews candidates, confirms, and renders the outcome. The
target root is always preserved, and only the directories confirmed at the
prompt are removed.
"""

from __future__ import annotations

from pathlib import Path

import click

from winnow.cli.console import console_from_context
from winnow.cli.errors import ExitCode
from winnow.cli.standards import dry_run_option, yes_option
from winnow.fs.empty_dirs import (
    BENIGN_SKIP_REASONS,
    remove_directories,
    scan_empty_directories,
)

__all__ = ["clean"]


def _directories(count: int) -> str:
    """Return ``directory`` or ``directories`` to match ``count``.

    Args:
        count: Number of directories being described.

    Returns:
        The singular or plural noun.
    """
    return "directory" if count == 1 else "directories"


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

    A confirmed directory that vanished or gained content is reported as
    skipped and the command still succeeds. Any other skip, such as a
    permission error or a path component swapped for a symlink, is reported
    the same way and exits with ``ExitCode.FAILURE``.
    """
    console = console_from_context(ctx)
    scan = scan_empty_directories(directory, exclude_patterns=exclude_patterns)
    candidates = scan.candidates

    if scan.unreadable:
        unreadable = len(scan.unreadable)
        console.print(
            f"{unreadable} {_directories(unreadable)} could not be read and "
            f"{'was' if unreadable == 1 else 'were'} left alone.",
        )

    if not candidates:
        console.print("No empty directories found.")
        return

    if dry_run:
        for candidate in candidates:
            console.print(f"Would remove: {candidate}")
        console.print(
            f"{len(candidates)} empty {_directories(len(candidates))} "
            "to remove (dry run).",
        )
        return

    if not yes and not click.confirm(
        f"Remove {len(candidates)} empty {_directories(len(candidates))}?",
    ):
        console.print("Aborted.")
        return

    result = remove_directories(candidates, root=directory)
    removed = len(result.removed)
    console.print(f"Removed {removed} empty {_directories(removed)}.")
    if result.skipped:
        skipped = len(result.skipped)
        console.print(f"Skipped {skipped} {_directories(skipped)}.")
        for path, reason in result.skipped:
            console.print(f"Skipped: {path} ({reason})")
    if any(reason not in BENIGN_SKIP_REASONS for _, reason in result.skipped):
        ctx.exit(ExitCode.FAILURE)
