"""``winnow prune`` command group for applying retention settings.

Planning and removal live in :mod:`winnow.fs.retention`; this module only
parses options, previews the plan, confirms, and renders the outcome.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import click

from winnow.cli.console import console_from_context
from winnow.cli.rendering import format_size
from winnow.cli.standards import config_path_option, dry_run_option, yes_option
from winnow.config import load_config
from winnow.fs.retention import plan_backup_prune, prune_backups

__all__ = ["prune"]


@click.group(name="prune")
def prune() -> None:
    """Remove data that has outlived its configured retention."""


@prune.command(name="backups")
@click.argument(
    "root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--max-age-days",
    type=click.IntRange(min=0),
    default=None,
    help="Remove backups older than this many days (default: config).",
)
@dry_run_option()
@yes_option()
@config_path_option()
@click.pass_context
def backups(
    ctx: click.Context,
    *,
    root: Path,
    max_age_days: int | None,
    dry_run: bool,
    yes: bool,
    config_path: Path | None,
) -> None:
    """Remove stale .winnow-backups files under ROOT.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        root: Directory whose backup directories are pruned.
        max_age_days: Age threshold in days; ``0`` disables pruning. Defaults
            to ``retention.backup_max_age_days`` from the configuration.
        dry_run: When set, list stale backups without deleting anything.
        yes: When set, skip the interactive confirmation prompt.
        config_path: Explicit configuration file path.

    Raises:
        FileSystemOperationError: If a stale backup cannot be removed.
        ConfigError: If the configuration cannot be loaded.
    """
    console = console_from_context(ctx)
    config = load_config(config_path=config_path)
    days = (
        max_age_days
        if max_age_days is not None
        else config.retention.backup_max_age_days
    )
    plan = plan_backup_prune(root, max_age=timedelta(days=days))

    if not plan.paths:
        console.print("No stale backups.")
        return

    for path in plan.paths:
        console.print(f"{path} ({format_size(path.stat().st_size)})")
    count = len(plan.paths)
    total = format_size(plan.bytes_total)

    if dry_run:
        console.print(f"{count} stale backup files ({total}) (dry run).")
        return

    if not yes and not click.confirm(f"Remove {count} backup files ({total})?"):
        console.print("Aborted.")
        return

    removed = prune_backups(plan)
    console.print(f"Removed {len(removed)} backup files ({total}).")
