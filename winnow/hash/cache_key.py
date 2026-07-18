"""Content-addressable cache key for perceptual hashes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from winnow.exceptions import CacheError
from winnow.models.enums import HashAlgorithm


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Identity of a cached perceptual hash.

    A cache entry is valid only while the file at ``path`` retains the recorded
    ``mtime`` and ``size``; any change to either invalidates the entry.

    Args:
        path: Filesystem path of the hashed media file.
        mtime: Modification time of the file, in seconds since the epoch.
        size: Size of the file in bytes.
        algorithm: Hash algorithm that produced the cached digest.
    """

    path: Path
    mtime: float
    size: int
    algorithm: HashAlgorithm

    @classmethod
    def from_file(
        cls,
        path: Path | str,
        algorithm: HashAlgorithm,
    ) -> Self:
        """Build a cache key by reading file metadata from disk.

        Args:
            path: Filesystem path of the media file to key on.
            algorithm: Hash algorithm the digest was produced with.

        Returns:
            A cache key populated with the file's current mtime and size.

        Raises:
            CacheError: If the file metadata cannot be read.
        """
        resolved = Path(path)
        try:
            stat = resolved.stat()
        except OSError as exc:
            raise CacheError(
                "Unable to read file metadata for cache key",
                operation="cache_key.from_file",
                file_path=resolved,
            ) from exc
        return cls(
            path=resolved,
            mtime=stat.st_mtime,
            size=stat.st_size,
            algorithm=algorithm,
        )
