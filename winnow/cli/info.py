"""``winnow info`` command for inspecting a single media file.

Metadata is gathered by :func:`winnow.media.inspect_file`; this module only
parses the argument and renders the result. Image rows are shown only when
the file could be decoded, so the command degrades gracefully otherwise.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.table import Table

from winnow.cli.console import console_from_context
from winnow.cli.rendering import format_size, format_timestamp
from winnow.media.inventory import FileInfo, inspect_file

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["build_info_table", "info"]


def build_info_table(file_info: FileInfo) -> Table:
    """Build a Rich table describing a media file.

    Args:
        file_info: Metadata gathered by :func:`winnow.media.inspect_file`.

    Returns:
        A two-column table of metadata field names and values.
    """
    table = Table(title="Media file info", show_header=True)
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold")

    media_type = file_info.media_type.value if file_info.media_type else "unknown"
    table.add_row("Path", str(file_info.path))
    table.add_row("Name", file_info.path.name)
    table.add_row("Media type", media_type)
    table.add_row("Extension", file_info.extension or "(none)")
    table.add_row(
        "Size",
        f"{format_size(file_info.size_bytes)} ({file_info.size_bytes} bytes)",
    )
    table.add_row("Modified (UTC)", format_timestamp(file_info.modified))
    table.add_row("Accessed (UTC)", format_timestamp(file_info.accessed))
    table.add_row("Changed (UTC)", format_timestamp(file_info.changed))

    metadata = file_info.metadata
    if metadata is not None:
        table.add_row("Image format", metadata.image_format or "unknown")
        table.add_row("Color mode", metadata.color_mode or "unknown")
        if metadata.width is not None and metadata.height is not None:
            table.add_row("Dimensions", f"{metadata.width}x{metadata.height}")

    return table


@click.command()
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.pass_context
def info(ctx: click.Context, *, file: Path) -> None:
    """Show metadata for a single media FILE.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        file: Media file to inspect.
    """
    console: Console = console_from_context(ctx)
    console.print(build_info_table(inspect_file(file)))
