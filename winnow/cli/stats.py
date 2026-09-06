"""``winnow stats`` command for summarizing a directory of media.

Files are classified with the format registry and aggregated into counts per
media type, a total size, and the span of file modification times.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.table import Table

from winnow.cli.console import console_from_context
from winnow.cli.rendering import format_size, format_timestamp
from winnow.media.inventory import DirectoryStats, collect_directory_stats
from winnow.models.media import MediaType

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["build_stats_table", "stats"]


def build_stats_table(directory: Path, directory_stats: DirectoryStats) -> Table:
    """Build a Rich table summarizing directory statistics.

    Args:
        directory: Directory the statistics describe.
        directory_stats: Aggregated statistics to render.

    Returns:
        A two-column table of statistic names and values.
    """
    table = Table(title=f"Media stats for {directory}", show_header=True)
    table.add_column("Metric", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold")

    table.add_row("Total files", str(directory_stats.total_files))
    for media_type in MediaType:
        count = directory_stats.counts_by_type.get(media_type, 0)
        table.add_row(f"{media_type.value.capitalize()} files", str(count))
    table.add_row("Unknown files", str(directory_stats.unknown_count))
    table.add_row(
        "Total size",
        f"{format_size(directory_stats.total_bytes)} "
        f"({directory_stats.total_bytes} bytes)",
    )

    earliest = directory_stats.earliest_modified
    latest = directory_stats.latest_modified
    table.add_row(
        "Earliest modified (UTC)",
        format_timestamp(earliest) if earliest is not None else "n/a",
    )
    table.add_row(
        "Latest modified (UTC)",
        format_timestamp(latest) if latest is not None else "n/a",
    )

    return table


@click.command()
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--recursive/--no-recursive",
    default=True,
    show_default=True,
    help="Include files in subdirectories.",
)
@click.pass_context
def stats(ctx: click.Context, *, directory: Path, recursive: bool) -> None:
    """Summarize the media files under DIRECTORY.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        directory: Directory to summarize.
        recursive: When set, include files in subdirectories.
    """
    console: Console = console_from_context(ctx)
    directory_stats = collect_directory_stats(directory, recursive=recursive)
    console.print(build_stats_table(directory, directory_stats))
