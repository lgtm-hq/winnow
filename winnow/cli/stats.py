"""``winnow stats`` command for summarizing a directory of media.

Files are enumerated with Discovery's walk (hidden entries and winnow's
working folders skipped, the configured exclude patterns and symlink policy
applied), identified by content the way Discovery identifies them, and
aggregated into counts per media type, a total size, the span of file
modification times, extension checks, skips and read errors. ``--fast``
classifies by extension only and says so.

``--recursive/--no-recursive`` and ``--fast`` are purpose-specific to this
command and intentionally not part of ``winnow.cli.standards``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
from rich.table import Table

from winnow.cli.console import console_from_context
from winnow.cli.rendering import format_size, format_timestamp
from winnow.cli.standards import config_path_option
from winnow.config import load_config
from winnow.fs.walk import WalkEventKind, WalkPolicy
from winnow.media.inventory import (
    Classification,
    DirectoryStats,
    collect_directory_stats,
)
from winnow.models.media import MediaType

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["build_stats_table", "stats"]

EXTENSION_ONLY: Final[str] = "extension only"
"""Label for results classified by file name extension alone."""

_SKIP_LABELS: Final[dict[WalkEventKind, str]] = {
    WalkEventKind.HIDDEN: "hidden or winnow folder",
    WalkEventKind.EXCLUDED: "excluded by pattern",
    WalkEventKind.SYMLINK: "symlink",
    WalkEventKind.NOT_REGULAR: "not a regular file",
    WalkEventKind.DUPLICATE: "duplicate symlink target",
}
"""Row text for each skip reason, in display order."""


def _extension_count(count: int | None) -> str:
    """Render an extension-check count, which needs content detection.

    Args:
        count: The count, or ``None`` when classified by extension.

    Returns:
        The count, or a note that it was not computed.
    """
    return str(count) if count is not None else f"n/a ({EXTENSION_ONLY})"


def _skipped(directory_stats: DirectoryStats) -> str:
    """Render the skipped-entry total with its reasons.

    Args:
        directory_stats: Aggregated statistics.

    Returns:
        ``"0"``, or the total followed by each reason's count.
    """
    by_reason = directory_stats.skipped_by_reason
    total = sum(by_reason.values())
    if total == 0:
        return "0"
    parts = [
        f"{by_reason[kind]} {label}"
        for kind, label in _SKIP_LABELS.items()
        if by_reason.get(kind)
    ]
    return f"{total} ({', '.join(parts)})"


def _coverage(directory_stats: DirectoryStats) -> str:
    """Say whether every admitted entry was read.

    Args:
        directory_stats: Aggregated statistics.

    Returns:
        ``"complete"``, or ``"incomplete"`` with what was not read.
    """
    if directory_stats.complete:
        return "complete"
    return (
        "incomplete: contents of unreadable directories are not counted, "
        "unreadable files are not in the totals"
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

    by_content = directory_stats.classified_by is Classification.CONTENT
    table.add_row("Classified by", "content" if by_content else EXTENSION_ONLY)
    table.add_row("Total files", str(directory_stats.total_files))
    for media_type in MediaType:
        count = directory_stats.counts_by_type.get(media_type, 0)
        table.add_row(f"{media_type.value.capitalize()} files", str(count))
    table.add_row("Unknown files", str(directory_stats.unknown_count))
    table.add_row(
        "Extension does not match content",
        _extension_count(directory_stats.extension_mismatch_count),
    )
    table.add_row(
        "Extension gives no type",
        _extension_count(directory_stats.extension_untyped_count),
    )
    table.add_row("Unreadable files", str(directory_stats.unreadable_count))
    table.add_row(
        "Unreadable directories",
        str(directory_stats.unreadable_directory_count),
    )
    table.add_row("Skipped entries", _skipped(directory_stats))
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
    table.add_row("Coverage", _coverage(directory_stats))
    if directory_stats.unreadable_examples:
        table.add_row(
            "Unreadable file examples",
            "\n".join(str(path) for path in directory_stats.unreadable_examples),
        )
    if directory_stats.unreadable_directory_examples:
        table.add_row(
            "Unreadable directory examples",
            "\n".join(
                str(path) for path in directory_stats.unreadable_directory_examples
            ),
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
@click.option(
    "--fast",
    is_flag=True,
    default=False,
    help="Classify by file name extension only, without reading content.",
)
@config_path_option()
@click.pass_context
def stats(
    ctx: click.Context,
    *,
    directory: Path,
    recursive: bool,
    fast: bool,
    config_path: Path | None,
) -> None:
    """Summarize the media files under DIRECTORY.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        directory: Directory to summarize.
        recursive: When set, include files in subdirectories.
        fast: When set, classify by extension only.
        config_path: Explicit configuration file, or ``None`` to discover.
    """
    console: Console = console_from_context(ctx)
    config = load_config(config_path=config_path)
    directory_stats = collect_directory_stats(
        directory,
        policy=WalkPolicy.from_config(config, recursive=recursive),
        fast=fast,
    )
    console.print(build_stats_table(directory, directory_stats))
