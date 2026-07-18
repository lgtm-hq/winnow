"""Pigeonhole band index for Hamming-distance candidate generation.

Private helper module for the duplicate finder: given a set of equal-width hash
values and a distance threshold, produces only the pairs that can possibly lie
within the threshold instead of comparing all pairs. Each hash is split into
``threshold + 1`` contiguous bit bands; by the pigeonhole principle two hashes
within the threshold must agree exactly on at least one band, so only hashes
sharing a band bucket are ever paired.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from itertools import combinations


def candidate_pairs(
    *,
    values: list[int],
    bit_length: int,
    threshold: int,
) -> Iterator[tuple[int, int]]:
    """Yield distinct hash pairs that can lie within ``threshold`` bits.

    Every pair whose Hamming distance is at most ``threshold`` is guaranteed to
    be yielded; pairs that share no band bucket are skipped. Yielded pairs must
    still be distance-checked by the caller, since sharing a band does not imply
    closeness.

    Args:
        values: Distinct decoded hash values.
        bit_length: Bit length shared by every value.
        threshold: Maximum Hamming distance (inclusive) for a match.

    Yields:
        Candidate ``(value_a, value_b)`` pairs, each at most once.
    """
    if threshold <= 0 or len(values) < 2:
        return
    if threshold >= bit_length:
        yield from combinations(values, 2)
        return
    band_count = threshold + 1
    base_width, remainder = divmod(bit_length, band_count)
    seen: set[tuple[int, int]] = set()
    start = 0
    for band in range(band_count):
        width = base_width + (1 if band < remainder else 0)
        mask = (1 << width) - 1
        buckets: dict[int, list[int]] = defaultdict(list)
        for value in values:
            buckets[(value >> start) & mask].append(value)
        start += width
        for members in buckets.values():
            for pair in combinations(members, 2):
                if pair not in seen:
                    seen.add(pair)
                    yield pair


__all__ = ["candidate_pairs"]
