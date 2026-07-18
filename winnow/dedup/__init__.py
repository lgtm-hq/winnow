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
from winnow.dedup.quality import (
    QualityComparator,
    QualityWeights,
    RankedMedia,
    SharpnessProvider,
    laplacian_sharpness,
)

__all__ = [
    "DEFAULT_HASH_DISTANCE_THRESHOLD",
    "DuplicateFinder",
    "HashFormat",
    "HashedFile",
    "QualityComparator",
    "QualityWeights",
    "RankedMedia",
    "SharpnessProvider",
    "find_duplicates",
    "hamming_distance",
    "laplacian_sharpness",
    "parse_hash",
]
