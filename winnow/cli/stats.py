"""``winnow stats`` command for summarizing a directory of media.

Files are classified with the format registry and aggregated into counts per
media type, a total size, and the span of file modification times.

``--recursive/--no-recursive`` is purpose-specific to this command and
intentionally not part of ``winnow.cli.standards``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.table import Table

from winnow.cli.rendering import console_from_context, format_size, format_timestamp
from winnow.media.registry import DEFAULT_FORMAT_REGISTRY, FormatRegistry
from winnow.models.media import MediaType

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from rich.console import Console

__all__ = ["DirectoryStats", "collect_directory_stats", "stats"]


@dataclass(frozen=True, slots=True)
class DirectoryStats:
    """Aggregate statistics for a directory of media files."""

    total_files: int
    total_bytes: int
    counts_by_type: Mapping[MediaType, int]
    unknown_count: int
    earliest_modified: datetime | None
    latest_modified: datetime | None


def _iter_files(directory: Path, *, recursive: bool) -> Iterator[Path]:
    """Yield regular files under a directory.

    Args:
        directory: Directory to walk.
        recursive: When ``True``, descend into subdirectories.

    Yields:
        Paths to regular files (symlinks and special files excluded).
    """
    entries = directory.rglob("*") if recursive else directory.iterdir()
    for entry in entries:
        if entry.is_file() and not entry.is_symlink():
            yield entry


def collect_directory_stats(
    directory: Path,
    *,
    recursive: bool = True,
    registry: FormatRegistry | None = None,
) -> DirectoryStats:
    """Aggregate media statistics for a directory.

    Args:
        directory: Directory to summarize.
        recursive: When ``True``, include files in subdirectories.
        registry: Format registry used to classify files. Defaults to the
            shared process-wide registry.

    Returns:
        Aggregate counts, total size, and modification-time span. Timestamps
        are expressed in UTC.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    counts: dict[MediaType, int] = defaultdict(int)
    total_files = 0
    total_bytes = 0
    unknown_count = 0
    earliest: datetime | None = None
    latest: datetime | None = None

    for file_path in _iter_files(directory, recursive=recursive):
        stat_result = file_path.stat()
        total_files += 1
        total_bytes += stat_result.st_size

        media_type = active_registry.lookup(file_path.name)
        if media_type is None:
            unknown_count += 1
        else:
            counts[media_type] += 1

        modified = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
        if earliest is None or modified < earliest:
            earliest = modified
        if latest is None or modified > latest:
            latest = modified

    return DirectoryStats(
        total_files=total_files,
        total_bytes=total_bytes,
        counts_by_type=dict(counts),
        unknown_count=unknown_count,
        earliest_modified=earliest,
        latest_modified=latest,
    )


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
