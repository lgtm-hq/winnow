"""``winnow info`` command for inspecting a single media file.

Metadata is gathered best-effort from the standard library ``stat`` call and
the format registry. Image dimensions are read via Pillow only when it is
installed; otherwise the command degrades gracefully and omits those rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import click
from rich.table import Table

from winnow.cli.console import console_from_context
from winnow.cli.rendering import format_size, format_timestamp
from winnow.media.registry import (
    DEFAULT_FORMAT_REGISTRY,
    FormatRegistry,
    normalize_extension,
)
from winnow.models.media import MediaType

if TYPE_CHECKING:
    from rich.console import Console

__all__ = [
    "FileInfo",
    "ImageSummary",
    "collect_file_info",
    "info",
    "read_image_summary",
    "summarize_image",
]


class _ImageLike(Protocol):
    """Structural type for the subset of a Pillow image Winnow inspects."""

    @property
    def format(self) -> str | None:
        """Image container format, such as ``"JPEG"``."""

    @property
    def mode(self) -> str:
        """Pixel mode, such as ``"RGB"``."""

    @property
    def width(self) -> int:
        """Image width in pixels."""

    @property
    def height(self) -> int:
        """Image height in pixels."""


@dataclass(frozen=True, slots=True)
class ImageSummary:
    """Summary of image-specific metadata."""

    image_format: str
    mode: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class FileInfo:
    """Structured metadata describing a single media file."""

    path: Path
    media_type: MediaType | None
    extension: str
    size_bytes: int
    modified: datetime
    accessed: datetime
    changed: datetime
    image: ImageSummary | None


def summarize_image(image: _ImageLike) -> ImageSummary:
    """Build an :class:`ImageSummary` from an opened image object.

    Args:
        image: Object exposing Pillow-style ``format``, ``mode``, ``width``,
            and ``height`` attributes.

    Returns:
        The extracted image summary.
    """
    return ImageSummary(
        image_format=image.format or "unknown",
        mode=image.mode,
        width=image.width,
        height=image.height,
    )


def read_image_summary(path: Path) -> ImageSummary | None:
    """Read image dimensions and format via Pillow when available.

    Args:
        path: Path to the image file.

    Returns:
        An image summary, or ``None`` when Pillow is unavailable or the file
        cannot be read as an image.
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None

    try:  # pragma: no cover - exercised only when Pillow is installed
        with Image.open(path) as image:
            return summarize_image(image)
    except OSError:  # pragma: no cover - Pillow-only failure path
        return None


def collect_file_info(
    path: Path,
    *,
    registry: FormatRegistry | None = None,
) -> FileInfo:
    """Gather metadata describing a media file.

    Args:
        path: File to inspect.
        registry: Format registry used to classify the file. Defaults to the
            shared process-wide registry.

    Returns:
        Structured metadata for the file. Timestamps are expressed in UTC.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    stat_result = path.stat()
    media_type = active_registry.lookup(path.name)
    image = read_image_summary(path) if media_type is MediaType.IMAGE else None
    return FileInfo(
        path=path,
        media_type=media_type,
        extension=normalize_extension(path.name),
        size_bytes=stat_result.st_size,
        modified=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
        accessed=datetime.fromtimestamp(stat_result.st_atime, tz=UTC),
        changed=datetime.fromtimestamp(stat_result.st_ctime, tz=UTC),
        image=image,
    )


def build_info_table(file_info: FileInfo) -> Table:
    """Build a Rich table describing a media file.

    Args:
        file_info: Metadata gathered by :func:`collect_file_info`.

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

    if file_info.image is not None:
        table.add_row("Image format", file_info.image.image_format)
        table.add_row("Color mode", file_info.image.mode)
        table.add_row(
            "Dimensions",
            f"{file_info.image.width}x{file_info.image.height}",
        )

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
    file_info = collect_file_info(file)
    console.print(build_info_table(file_info))
