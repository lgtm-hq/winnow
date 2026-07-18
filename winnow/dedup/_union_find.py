"""Disjoint-set (union-find) structure used by the duplicate finder.

Private helper module: the class is generic over integer-indexed elements and
carries no dedup-specific behavior, so it lives apart from the finder logic.
"""

from __future__ import annotations


class UnionFind:
    """Disjoint-set structure with path compression and union by rank.

    Args:
        size: Number of singleton elements to track.
    """

    __slots__ = ("_parent", "_rank")

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, item: int) -> int:
        """Return the representative root of ``item``.

        Args:
            item: Element index.

        Returns:
            Root index of the set containing ``item``.
        """
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, first: int, second: int) -> None:
        """Merge the sets containing ``first`` and ``second``.

        Args:
            first: First element index.
            second: Second element index.
        """
        root_first, root_second = self.find(first), self.find(second)
        if root_first == root_second:
            return
        if self._rank[root_first] < self._rank[root_second]:
            root_first, root_second = root_second, root_first
        self._parent[root_second] = root_first
        if self._rank[root_first] == self._rank[root_second]:
            self._rank[root_first] += 1


__all__ = ["UnionFind"]
