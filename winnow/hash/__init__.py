"""Hash domain: perceptual hashing and content-addressable caches.

Perceptual hashes have two canonical string forms:

- **Self-describing**: :meth:`PerceptualHash.serialize` returns
  ``"{algorithm}:{hash_size}:{hex}"``. Use it wherever a hash is persisted
  without its parameters alongside (cache values, report columns, JSON output).
- **Bare**: :attr:`PerceptualHash.digest` is the hex digest alone. Use it
  in-process where the algorithm is fixed by context.

Writers emit one of these two forms; every reader goes through
:func:`parse_digest`, which accepts both.
"""

from __future__ import annotations

from winnow.hash.cache import HashCache, open_hash_cache
from winnow.hash.cache_entry import CacheEntry
from winnow.hash.cache_key import CacheKey
from winnow.hash.cache_stats import CacheStats
from winnow.hash.digest import hamming_distance, parse_digest
from winnow.hash.image_hasher import (
    DEFAULT_HASH_SIZE,
    PERCEPTUAL_ALGORITHMS,
    ImageHasher,
    PerceptualHash,
    hash_image,
)
from winnow.hash.protocol import PerceptualHasher

__all__ = [
    "DEFAULT_HASH_SIZE",
    "PERCEPTUAL_ALGORITHMS",
    "CacheEntry",
    "CacheKey",
    "CacheStats",
    "HashCache",
    "ImageHasher",
    "PerceptualHash",
    "PerceptualHasher",
    "hamming_distance",
    "hash_image",
    "open_hash_cache",
    "parse_digest",
]
