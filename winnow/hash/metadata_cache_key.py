"""Cache key for persisted media metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from winnow.exceptions import CacheError


@dataclass(frozen=True, slots=True)
class MetadataCacheKey:
    """Identity of a cached :class:`~winnow.models.media.MediaMetadata` row.

    A cache entry is valid only while the file at ``path`` retains the recorded
    ``mtime`` and ``size``; any change to either invalidates the entry.
    :meth:`from_file` stores an absolute, resolved path so cache identity is
    stable regardless of the working directory in effect at lookup time. The
    resolution and error semantics match :class:`winnow.hash.cache_key.CacheKey`.

    Args:
        path: Filesystem path of the media file.
        mtime: Modification time of the file, in seconds since the epoch.
        size: Size of the file in bytes.
    """

    path: Path
    mtime: float
    size: int

    @classmethod
    def from_file(cls, path: Path | str) -> Self:
        """Build a cache key by reading file metadata from disk.

        Args:
            path: Filesystem path of the media file to key on.

        Returns:
            A cache key populated with the file's current mtime and size.

        Raises:
            CacheError: If the file metadata cannot be read.
        """
        resolved = Path(path).resolve()
        try:
            stat = resolved.stat()
        except OSError as exc:
            raise CacheError(
                "Unable to read file metadata for cache key",
                operation="metadata_cache_key.from_file",
                file_path=resolved,
            ) from exc
        return cls(path=resolved, mtime=stat.st_mtime, size=stat.st_size)
