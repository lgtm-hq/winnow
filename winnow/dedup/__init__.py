"""Duplicate detection and quality-comparison domain.

Exposes the perceptual-hash duplicate finder (:class:`DuplicateFinder`) and the
quality comparator (:class:`QualityComparator`) used to group similar media files
and select the best copy to keep.
"""

from __future__ import annotations

from winnow.dedup.finder import (
    DEFAULT_HASH_DISTANCE_THRESHOLD,
    DuplicateFinder,
    HashedFile,
    find_duplicates,
)
from winnow.dedup.hashing import HashFormat, hamming_distance, parse_hash

__all__ = [
    "DEFAULT_HASH_DISTANCE_THRESHOLD",
    "DuplicateFinder",
    "HashFormat",
    "HashedFile",
    "find_duplicates",
    "hamming_distance",
    "parse_hash",
]
