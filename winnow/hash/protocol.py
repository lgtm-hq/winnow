"""Structural interface shared by every perceptual hasher.

ADR 0002 requires the hashing interface to sit behind a Protocol from day one
so image, video, and future native backends are interchangeable for the batch
hasher, the deduplication step, and the hash cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from winnow.hash.image_hasher import PerceptualHash


@runtime_checkable
class PerceptualHasher(Protocol):
    """Hash a media file into a fixed-length :class:`PerceptualHash`.

    Implementations are expected to be immutable: :attr:`cache_algorithm`
    must stay constant for the lifetime of an instance because it is baked
    into :class:`~winnow.hash.cache_key.CacheKey`.
    """

    @property
    def cache_algorithm(self) -> str:
        """Identity string for the hash cache.

        Two hashers whose outputs are not comparable bit-for-bit must return
        different strings; the value encodes the algorithm and every
        parameter that changes the digest (for example ``"phash:8"``).

        Returns:
            Stable, human-readable hasher identity.
        """
        ...

    def hash_file(self, path: Path) -> PerceptualHash:
        """Compute the perceptual hash of the media file at ``path``.

        Args:
            path: Filesystem path of the media file to hash.

        Returns:
            Computed perceptual hash.

        Raises:
            HashError: If the file cannot be read, decoded, or hashed.
        """
        ...
