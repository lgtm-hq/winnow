"""``winnow live-photos`` command for reporting Apple Live Photo pairs.

The command is a thin adapter over :func:`winnow.classify.detect_live_photos`:
it scans a directory, then renders the resulting :class:`LivePhotoScan` as a
Rich table or JSON. Pairing rules and directory walking live in
:mod:`winnow.classify.livephoto`, never here.

``--recursive/--no-recursive`` is purpose-specific to this command and
intentionally not part of ``winnow.cli.standards``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.table import Table

from winnow.classify import LivePhotoScan, detect_live_photos
from winnow.cli.console import console_from_context
from winnow.cli.standards import OutputFormat, format_option

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["build_pairs_table", "build_unpaired_table", "live_photos", "scan_to_dict"]

_SUPPORTED_FORMATS: frozenset[OutputFormat] = frozenset(
    {OutputFormat.TABLE, OutputFormat.JSON},
)


def build_pairs_table(directory: Path, scan: LivePhotoScan) -> Table:
    """Build a Rich table listing the pairs found in a scan.

    Args:
        directory: Directory the scan describes.
        scan: Scan result to render.

    Returns:
        A four-column table of still, video, verification state, and identifier.
    """
    table = Table(title=f"Live Photo pairs in {directory}", show_header=True)
    table.add_column("Still", overflow="fold")
    table.add_column("Video", overflow="fold")
    table.add_column("Verified", no_wrap=True)
    table.add_column("Content Identifier", overflow="fold")
    for pair in scan.pairs:
        table.add_row(
            str(pair.still),
            str(pair.video),
            "yes" if pair.verified else "no",
            pair.content_identifier or "",
        )
    return table


def build_unpaired_table(directory: Path, scan: LivePhotoScan) -> Table:
    """Build a Rich table listing the orphans found in a scan.

    Args:
        directory: Directory the scan describes.
        scan: Scan result to render.

    Returns:
        A two-column table of path and kind (``still`` or ``video``).
    """
    table = Table(title=f"Unpaired Live Photo files in {directory}", show_header=True)
    table.add_column("Path", overflow="fold")
    table.add_column("Kind", no_wrap=True)
    for still in scan.unpaired_stills:
        table.add_row(str(still), "still")
    for video in scan.unpaired_videos:
        table.add_row(str(video), "video")
    return table


def scan_to_dict(scan: LivePhotoScan) -> dict[str, Any]:
    """Convert a scan into a JSON-serializable dictionary with string paths.

    Args:
        scan: Scan result to convert.

    Returns:
        A dictionary with ``pairs``, ``unpaired_stills`` and ``unpaired_videos``.
    """
    return {
        "pairs": [
            {
                "still": str(pair.still),
                "video": str(pair.video),
                "content_identifier": pair.content_identifier,
                "verified": pair.verified,
            }
            for pair in scan.pairs
        ],
        "unpaired_stills": [str(path) for path in scan.unpaired_stills],
        "unpaired_videos": [str(path) for path in scan.unpaired_videos],
    }


@click.command(name="live-photos")
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
    "--unpaired",
    is_flag=True,
    default=False,
    help="List unpaired stills and videos instead of pairs.",
)
@format_option()
@click.pass_context
def live_photos(
    ctx: click.Context,
    *,
    directory: Path,
    recursive: bool,
    unpaired: bool,
    format: str,
) -> None:
    """Report Apple Live Photo pairs under DIRECTORY.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        directory: Directory to scan.
        recursive: When set, include files in subdirectories.
        unpaired: When set, list orphans instead of pairs.
        format: Output format; only ``table`` and ``json`` are supported.

    Raises:
        click.UsageError: If ``format`` is not ``table`` or ``json``.
    """
    output_format = OutputFormat(format)
    if output_format not in _SUPPORTED_FORMATS:
        raise click.UsageError("format not supported by live-photos")

    scan = detect_live_photos(directory, recursive=recursive)
    if output_format is OutputFormat.JSON:
        click.echo(json.dumps(scan_to_dict(scan), indent=2))
        return

    console: Console = console_from_context(ctx)
    builder = build_unpaired_table if unpaired else build_pairs_table
    console.print(builder(directory, scan))
