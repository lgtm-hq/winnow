"""Full cache entry pairing a key with its stored digest."""

from __future__ import annotations

from dataclasses import dataclass

from winnow.hash.cache_key import CacheKey


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cache key together with the perceptual hash digest it maps to.

    Args:
        key: Identity of the cached hash, including file metadata.
        digest: Perceptual hash digest stored for the key.
    """

    key: CacheKey
    digest: str
