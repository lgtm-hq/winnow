"""``winnow cache`` command group for inspecting and managing the hash cache.

All cache logic lives on :class:`winnow.hash.cache.HashCache`; this module
only resolves the database path from configuration, previews, confirms, and
renders the outcome, following the destructive-command pattern used by
``winnow clean``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from winnow.cli.console import console_from_context
from winnow.cli.rendering import format_size
from winnow.cli.standards import config_path_option, dry_run_option, yes_option
from winnow.config import load_config
from winnow.hash._db import default_db_path
from winnow.hash.cache import HashCache

__all__ = ["cache"]


def _resolve_db_path(config_path: Path | None) -> Path:
    """Resolve the hash cache database path from the effective configuration.

    Args:
        config_path: Explicit configuration file path, or ``None`` to discover.

    Returns:
        ``cache.directory / "cache.db"`` for the loaded configuration.

    Raises:
        ConfigError: If the configuration cannot be loaded.
    """
    return default_db_path(load_config(config_path=config_path).cache)


@contextmanager
def _existing_cache(
    ctx: click.Context,
    config_path: Path | None,
) -> Iterator[tuple[Console, HashCache | None]]:
    """Yield the console and an open cache, or ``None`` when no database exists.

    A missing database is reported as ``no cache database at <path>`` and the
    cache is never created as a side effect of inspecting it.

    Args:
        ctx: Active Click context carrying shared options.
        config_path: Explicit configuration file path.

    Yields:
        The console for this command and the open cache or ``None``.
    """
    console = console_from_context(ctx)
    db_path = _resolve_db_path(config_path)
    if not db_path.exists():
        console.print(f"no cache database at {db_path}", soft_wrap=True)
        yield console, None
        return
    with HashCache(db_path=db_path) as hash_cache:
        yield console, hash_cache


def _confirmed(console: Console, *, prompt: str, yes: bool) -> bool:
    """Ask for confirmation unless ``--yes`` was given.

    Args:
        console: Console used to report a decline.
        prompt: Question shown to the user.
        yes: When set, skip the prompt and confirm.

    Returns:
        ``True`` to proceed; ``False`` after printing ``Aborted.``.
    """
    if yes or click.confirm(prompt):
        return True
    console.print("Aborted.")
    return False


def _render_summary(
    console: Console,
    *,
    entry_count: int,
    footer: str,
) -> None:
    """Render the ``Cache`` table followed by a one-line footer.

    Args:
        console: Console used to render the table.
        entry_count: Number of rows in the hash table.
        footer: Line printed beneath the table; never wrapped so the path
            stays on one line.
    """
    table = Table(title="Cache")
    table.add_column("Table")
    table.add_column("Entries", justify="right")
    table.add_row("hash", str(entry_count))
    console.print(table)
    console.print(footer, soft_wrap=True)


@click.group(name="cache")
def cache() -> None:
    """Inspect and manage the perceptual-hash cache database."""


@cache.command(name="show")
@config_path_option()
@click.pass_context
def show(ctx: click.Context, *, config_path: Path | None) -> None:
    """Show cached entry counts and the database size.

    Hit rate is not shown: hit and miss counters are per process and reset
    every time the cache is opened.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        config_path: Explicit configuration file path.
    """
    console = console_from_context(ctx)
    db_path = _resolve_db_path(config_path)
    if not db_path.exists():
        _render_summary(
            console,
            entry_count=0,
            footer=f"no cache database at {db_path}",
        )
        return
    with HashCache(db_path=db_path) as hash_cache:
        stats = hash_cache.stats()
    _render_summary(
        console,
        entry_count=stats.entry_count,
        footer=f"Database: {db_path} ({format_size(stats.size_bytes)})",
    )


@cache.command(name="clear")
@config_path_option()
@dry_run_option()
@yes_option()
@click.pass_context
def clear(
    ctx: click.Context,
    *,
    config_path: Path | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Remove every entry from the hash cache.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        config_path: Explicit configuration file path.
        dry_run: When set, report the entry count without deleting anything.
        yes: When set, skip the interactive confirmation prompt.
    """
    with _existing_cache(ctx, config_path) as (console, hash_cache):
        if hash_cache is None:
            return
        count = hash_cache.stats().entry_count
        if count == 0:
            console.print("Nothing to remove.")
            return
        if dry_run:
            console.print(f"Would remove {count} entries.")
            return
        if not _confirmed(console, prompt=f"Remove {count} cached entries?", yes=yes):
            return
        hash_cache.clear()
        console.print(f"Removed {count} entries.")


@cache.command(name="prune")
@config_path_option()
@dry_run_option()
@yes_option()
@click.pass_context
def prune(
    ctx: click.Context,
    *,
    config_path: Path | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Remove cached entries whose source files no longer exist.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        config_path: Explicit configuration file path.
        dry_run: When set, list stale paths without deleting anything.
        yes: When set, skip the interactive confirmation prompt.
    """
    with _existing_cache(ctx, config_path) as (console, hash_cache):
        if hash_cache is None:
            return
        stale = hash_cache.stale_paths()
        if not stale:
            console.print("No stale entries.")
            return
        if dry_run:
            for path in stale:
                console.print(path, soft_wrap=True)
            console.print(f"{len(stale)} stale entries (dry run).")
            return
        if not _confirmed(console, prompt=f"Prune {len(stale)} stale entries?", yes=yes):
            return
        pruned = hash_cache.prune_stale()
        console.print(f"Pruned {pruned} entries.")
