"""``winnow info`` command for inspecting a single media file.

Metadata is gathered by :func:`winnow.media.inspect_file`; this module only
parses the argument and renders the result. The media type comes from
:func:`winnow.media.detect`, the way Discovery decides it: from the content,
or from the extension when no content rule recognises the file, which the
output says. An "Extension check" row says whether the name agrees. Image,
video and audio rows are shown only when the file's metadata could be read,
so the command degrades gracefully otherwise. ``--fast`` classifies by
extension only, reads no content and no metadata, and says so.

The argument is checked for existence and kind only; readability is left to
the command, so an unreadable file exits 1 with a ``MediaError`` rather than
a usage error.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
from rich.table import Table

from winnow.cli.console import console_from_context
from winnow.cli.rendering import format_size, format_timestamp
from winnow.media.inventory import (
    ExtensionCheck,
    FileInfo,
    extension_check,
    inspect_file,
)
from winnow.models.media import MediaMetadata, MediaType

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["build_info_table", "info"]

EXTENSION_ONLY: Final[str] = "extension only"
"""Label for results classified by file name extension alone."""

FROM_NAME: Final[str] = "from the file name"
"""Label for a type detection took from the name, no content rule matching."""


def _type_name(media_type: MediaType | None) -> str:
    """Name a media type for display.

    Args:
        media_type: The type, or ``None``.

    Returns:
        The type's value, or ``"unknown"``.
    """
    return media_type.value if media_type is not None else "unknown"


def _extension_check_text(file_info: FileInfo) -> str:
    """Describe how the file's extension compares with its content.

    Args:
        file_info: Inspected file.

    Returns:
        One line for the "Extension check" row.
    """
    detection = file_info.detection
    if detection is None:
        return f"not checked ({EXTENSION_ONLY})"
    content = _type_name(detection.media_type)
    named = _type_name(detection.extension_media_type)
    if detection.extension_media_type is not None:
        unknown_content = f"content gives no type; extension says {named}"
    else:
        unknown_content = "content gives no type; extension gives no type"
    texts = {
        ExtensionCheck.MATCH: "matches content",
        ExtensionCheck.MISMATCH: (
            f"mismatch: content says {content}, extension says {named}"
        ),
        ExtensionCheck.EXTENSION_UNTYPED: "extension gives no type",
        ExtensionCheck.NAME_ONLY: f"type is {FROM_NAME}, not the content",
        ExtensionCheck.CONTENT_UNKNOWN: unknown_content,
    }
    return texts[extension_check(detection)]


def _add_image_rows(table: Table, metadata: MediaMetadata) -> None:
    """Add the image rows.

    Args:
        table: Table to extend.
        metadata: Image metadata.
    """
    table.add_row("Image format", metadata.image_format or "unknown")
    table.add_row("Color mode", metadata.color_mode or "unknown")
    if metadata.width is not None and metadata.height is not None:
        table.add_row("Dimensions", f"{metadata.width}x{metadata.height}")


def _add_stream_rows(table: Table, metadata: MediaMetadata) -> None:
    """Add the video and audio rows that were read.

    Args:
        table: Table to extend.
        metadata: Video or audio metadata.
    """
    if metadata.duration_seconds is not None:
        table.add_row("Duration", f"{metadata.duration_seconds:.3f} s")
    if metadata.codec is not None:
        table.add_row("Codec", metadata.codec)


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

    media_type = _type_name(file_info.media_type)
    detection = file_info.detection
    if detection is None:
        media_type = f"{media_type} ({EXTENSION_ONLY})"
    elif extension_check(detection) is ExtensionCheck.NAME_ONLY:
        media_type = f"{media_type} ({FROM_NAME})"
    table.add_row("Path", str(file_info.path))
    table.add_row("Name", file_info.path.name)
    table.add_row("Media type", media_type)
    table.add_row("Extension", file_info.extension or "(none)")
    table.add_row("Extension check", _extension_check_text(file_info))
    table.add_row(
        "Size",
        f"{format_size(file_info.size_bytes)} ({file_info.size_bytes} bytes)",
    )
    table.add_row("Modified (UTC)", format_timestamp(file_info.modified))
    table.add_row("Accessed (UTC)", format_timestamp(file_info.accessed))
    table.add_row("Changed (UTC)", format_timestamp(file_info.changed))

    metadata = file_info.metadata
    if metadata is None and detection is None:
        table.add_row("Metadata", f"not read ({EXTENSION_ONLY})")
    elif metadata is not None:
        if file_info.media_type is MediaType.IMAGE:
            _add_image_rows(table, metadata)
        else:
            _add_stream_rows(table, metadata)

    return table


@click.command()
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, readable=False, path_type=Path),
)
@click.option(
    "--fast",
    is_flag=True,
    default=False,
    help="Type by file name extension only; read no content and no metadata.",
)
@click.pass_context
def info(ctx: click.Context, *, file: Path, fast: bool) -> None:
    """Show metadata for a single media FILE.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        file: Media file to inspect.
        fast: When set, classify by extension only.
    """
    console: Console = console_from_context(ctx)
    console.print(build_info_table(inspect_file(file, fast=fast)))
