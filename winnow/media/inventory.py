"""Directory inventory and per-file inspection.

Files are classified with the format registry. Directory inventories aggregate
counts per media type, a total size, and the span of file modification times;
single-file inspection adds best-effort structural metadata for images.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from winnow.exceptions import MediaError
from winnow.media.image import extract_image_metadata
from winnow.media.registry import (
    DEFAULT_FORMAT_REGISTRY,
    FormatRegistry,
    normalize_extension,
)
from winnow.models.media import MediaMetadata, MediaType

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = [
    "DirectoryStats",
    "FileInfo",
    "collect_directory_stats",
    "inspect_file",
    "iter_regular_files",
]


@dataclass(frozen=True, slots=True)
class DirectoryStats:
    """Aggregate statistics for a directory of media files."""

    total_files: int
    total_bytes: int
    counts_by_type: Mapping[MediaType, int]
    unknown_count: int
    earliest_modified: datetime | None
    latest_modified: datetime | None


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
    metadata: MediaMetadata | None


def iter_regular_files(directory: Path, *, recursive: bool = True) -> Iterator[Path]:
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

    for file_path in iter_regular_files(directory, recursive=recursive):
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


def _image_metadata_or_none(path: Path) -> MediaMetadata | None:
    """Extract image metadata, degrading to ``None`` when it cannot be read.

    Args:
        path: Image file to inspect.

    Returns:
        The extracted metadata, or ``None`` when the file cannot be decoded.
    """
    try:
        return extract_image_metadata(path)
    except MediaError:
        return None


def inspect_file(
    path: Path,
    *,
    registry: FormatRegistry | None = None,
) -> FileInfo:
    """Gather metadata describing a single file.

    Image files additionally carry structural metadata (dimensions, format,
    color mode) when they can be decoded. Other media types carry no
    metadata yet.

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
    metadata = _image_metadata_or_none(path) if media_type is MediaType.IMAGE else None
    return FileInfo(
        path=path,
        media_type=media_type,
        extension=normalize_extension(path.name),
        size_bytes=stat_result.st_size,
        modified=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
        accessed=datetime.fromtimestamp(stat_result.st_atime, tz=UTC),
        changed=datetime.fromtimestamp(stat_result.st_ctime, tz=UTC),
        metadata=metadata,
    )
