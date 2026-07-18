"""Hash domain: perceptual hashing and content-addressable caches."""

from __future__ import annotations

from winnow.hash.cache import HashCache
from winnow.hash.cache_entry import CacheEntry
from winnow.hash.cache_key import CacheKey
from winnow.hash.cache_stats import CacheStats
from winnow.hash.image_hasher import (
    DEFAULT_HASH_SIZE,
    PERCEPTUAL_ALGORITHMS,
    ImageHasher,
    PerceptualHash,
    hamming_distance,
    hash_image,
)

__all__ = [
    "DEFAULT_HASH_SIZE",
    "PERCEPTUAL_ALGORITHMS",
    "CacheEntry",
    "CacheKey",
    "CacheStats",
    "HashCache",
    "ImageHasher",
    "PerceptualHash",
    "hamming_distance",
    "hash_image",
]
