"""Aggregate statistics for a hash cache."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Point-in-time metrics describing hash cache activity and size.

    Args:
        hits: Number of lookups served from the cache this session.
        misses: Number of lookups that found no valid entry this session.
        entry_count: Number of rows currently stored in the cache.
        size_bytes: On-disk size of the cache database in bytes.
    """

    hits: int
    misses: int
    entry_count: int
    size_bytes: int

    @property
    def lookups(self) -> int:
        """Return the total number of lookups performed this session.

        Returns:
            Sum of hits and misses.
        """
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Return the fraction of lookups served from the cache.

        Returns:
            Hits divided by total lookups, or ``0.0`` when no lookups occurred.
        """
        total = self.lookups
        if total == 0:
            return 0.0
        return self.hits / total
