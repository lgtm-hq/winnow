"""Perceptual-hash duplicate finder.

Groups media files whose perceptual hashes fall within a configurable Hamming
distance threshold. Grouping uses union-find so that transitively similar files
(``a ~ b`` and ``b ~ c``) collapse into a single :class:`DuplicateGroup` even when
``a`` and ``c`` are not directly within threshold.

Hashes are only compared within the same :class:`~winnow.models.media.MediaType`
and the same bit length, so mixing hash widths or media types never produces
cross-type matches.

Near-match candidates are generated with a pigeonhole band index rather than
all-pairs comparison: each hash is split into ``threshold + 1`` bit bands, and
two hashes within the threshold must agree exactly on at least one band, so only
hashes sharing a band bucket are ever compared. For realistic libraries — where
most hashes are unique — this keeps grouping near-linear instead of quadratic in
the number of distinct hashes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from winnow.dedup._band_index import candidate_pairs
from winnow.dedup._union_find import UnionFind
from winnow.exceptions import DuplicateError, HashError
from winnow.hash.digest import parse_digest
from winnow.models.config import WinnowConfig
from winnow.models.duplicates import DuplicateGroup, DuplicatePair
from winnow.models.media import MediaType

DEFAULT_HASH_DISTANCE_THRESHOLD = 10


@dataclass(frozen=True, slots=True)
class HashedFile:
    """A media file paired with its perceptual hash.

    Attributes:
        path: Absolute or relative path to the media file.
        perceptual_hash: Perceptual hash of the file, either the bare hex
            digest or a ``PerceptualHash.serialize()`` string.
        media_type: Media type used to partition comparisons.
    """

    path: Path
    perceptual_hash: str
    media_type: MediaType


@dataclass(frozen=True, slots=True)
class _Edge:
    """A discovered duplicate relationship between two file indices."""

    index_a: int
    index_b: int
    similarity: float


class DuplicateFinder:
    """Group perceptually similar media files into duplicate groups.

    Args:
        threshold: Maximum Hamming distance (inclusive) for two hashes to be
            considered duplicates.

    Raises:
        DuplicateError: If ``threshold`` is negative.
    """

    __slots__ = ("threshold",)

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_HASH_DISTANCE_THRESHOLD,
    ) -> None:
        if threshold < 0:
            raise DuplicateError(
                "duplicate distance threshold must be non-negative",
                operation="DuplicateFinder",
                details={"threshold": threshold},
            )
        self.threshold = threshold

    @classmethod
    def from_config(
        cls,
        config: WinnowConfig | None = None,
    ) -> Self:
        """Build a finder from a :class:`WinnowConfig`.

        Args:
            config: Configuration providing
                ``perceptual_hash_distance_threshold``. When ``None`` the module
                default of ``10`` is used.

        Returns:
            Configured duplicate finder.
        """
        threshold = (
            DEFAULT_HASH_DISTANCE_THRESHOLD
            if config is None
            else config.perceptual_hash_distance_threshold
        )
        return cls(threshold=threshold)

    def find(self, files: Iterable[HashedFile]) -> list[DuplicateGroup]:
        """Group similar files into duplicate groups sorted by size.

        Args:
            files: Media files paired with their perceptual hashes.

        Returns:
            Duplicate groups (each with two or more members) ordered by member
            count descending, then by the lexicographically smallest path.

        Raises:
            DuplicateError: If a hash cannot be parsed or a path appears twice.
        """
        items = list(files)
        self._reject_duplicate_paths(items)
        decoded = self._decode(items)
        partitions = _partition(items=items, decoded=decoded)

        groups: list[DuplicateGroup] = []
        for (media_type, bit_length), indices in partitions.items():
            groups.extend(
                self._group_partition(
                    items=items,
                    decoded=decoded,
                    media_type=media_type,
                    bit_length=bit_length,
                    indices=indices,
                ),
            )

        groups.sort(key=lambda group: (-len(group.files), _sort_key(group)))
        for number, group in enumerate(groups, start=1):
            group.group_number = number
        return groups

    def _decode(self, items: list[HashedFile]) -> list[tuple[int, int]]:
        """Decode every perceptual hash into ``(value, bit_length)``.

        Args:
            items: Parsed input files.

        Returns:
            Decoded ``(value, bit_length)`` pairs aligned with ``items``.

        Raises:
            DuplicateError: If any perceptual hash cannot be parsed.
        """
        decoded: list[tuple[int, int]] = []
        for item in items:
            try:
                decoded.append(parse_digest(item.perceptual_hash))
            except HashError as error:
                raise DuplicateError(
                    "failed to parse perceptual hash",
                    operation="DuplicateFinder.find",
                    file_path=item.path,
                ) from error
        return decoded

    def _group_partition(
        self,
        *,
        items: list[HashedFile],
        decoded: list[tuple[int, int]],
        media_type: MediaType,
        bit_length: int,
        indices: list[int],
    ) -> list[DuplicateGroup]:
        """Group a single media-type/bit-length partition.

        Args:
            items: All parsed input files.
            decoded: Decoded ``(value, bit_length)`` pairs aligned with ``items``.
            media_type: Media type shared by every index in the partition.
            bit_length: Hash bit length shared by every index in the partition.
            indices: Global indices belonging to the partition.

        Returns:
            Duplicate groups discovered within the partition (unnumbered).
        """
        union_find = UnionFind(len(items))
        edges: list[_Edge] = []
        buckets = _build_buckets(decoded=decoded, indices=indices)
        _link_exact_duplicates(buckets=buckets, union_find=union_find, edges=edges)
        self._link_similar_hashes(
            buckets=buckets,
            bit_length=bit_length,
            union_find=union_find,
            edges=edges,
        )
        return _assemble_groups(
            items=items,
            media_type=media_type,
            indices=indices,
            union_find=union_find,
            edges=edges,
        )

    def _link_similar_hashes(
        self,
        *,
        buckets: dict[int, list[int]],
        bit_length: int,
        union_find: UnionFind,
        edges: list[_Edge],
    ) -> None:
        """Union bucket representatives within the distance threshold.

        Args:
            buckets: Mapping of hash value to member indices.
            bit_length: Hash bit length shared by every bucket.
            union_find: Disjoint-set structure to update in place.
            edges: Edge list to append near-match relationships to.
        """
        representatives = {value: members[0] for value, members in buckets.items()}
        candidates = candidate_pairs(
            values=list(representatives),
            bit_length=bit_length,
            threshold=self.threshold,
        )
        for value_a, value_b in candidates:
            distance = (value_a ^ value_b).bit_count()
            if distance > self.threshold:
                continue
            index_a = representatives[value_a]
            index_b = representatives[value_b]
            union_find.union(index_a, index_b)
            edges.append(
                _Edge(
                    index_a=index_a,
                    index_b=index_b,
                    similarity=_similarity(distance=distance, bit_length=bit_length),
                ),
            )

    @staticmethod
    def _reject_duplicate_paths(items: list[HashedFile]) -> None:
        """Ensure no file path appears more than once.

        Args:
            items: Parsed input files.

        Raises:
            DuplicateError: If a path is provided more than once.
        """
        seen: set[Path] = set()
        for item in items:
            if item.path in seen:
                raise DuplicateError(
                    "duplicate path provided to duplicate finder",
                    operation="DuplicateFinder.find",
                    file_path=item.path,
                )
            seen.add(item.path)


def find_duplicates(
    files: Iterable[HashedFile],
    *,
    threshold: int = DEFAULT_HASH_DISTANCE_THRESHOLD,
) -> list[DuplicateGroup]:
    """Group similar files using a one-off :class:`DuplicateFinder`.

    Args:
        files: Media files paired with their perceptual hashes.
        threshold: Maximum Hamming distance (inclusive) for a match.

    Returns:
        Duplicate groups ordered by member count descending.
    """
    finder = DuplicateFinder(threshold=threshold)
    return finder.find(files)


def _partition(
    *,
    items: list[HashedFile],
    decoded: list[tuple[int, int]],
) -> dict[tuple[MediaType, int], list[int]]:
    """Partition indices by media type and hash bit length.

    Args:
        items: Parsed input files.
        decoded: Decoded ``(value, bit_length)`` pairs aligned with ``items``.

    Returns:
        Mapping of ``(media_type, bit_length)`` to item indices.
    """
    partitions: dict[tuple[MediaType, int], list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        _value, bit_length = decoded[index]
        partitions[(item.media_type, bit_length)].append(index)
    return partitions


def _build_buckets(
    *,
    decoded: list[tuple[int, int]],
    indices: list[int],
) -> dict[int, list[int]]:
    """Bucket partition indices by decoded hash value.

    Args:
        decoded: Decoded ``(value, bit_length)`` pairs aligned with the inputs.
        indices: Global indices belonging to the partition.

    Returns:
        Mapping of decoded hash integer to the indices sharing that value.
    """
    buckets: dict[int, list[int]] = defaultdict(list)
    for index in indices:
        value, _bits = decoded[index]
        buckets[value].append(index)
    return buckets


def _link_exact_duplicates(
    *,
    buckets: dict[int, list[int]],
    union_find: UnionFind,
    edges: list[_Edge],
) -> None:
    """Union files with identical hashes and record exact-match edges.

    Args:
        buckets: Mapping of hash value to member indices.
        union_find: Disjoint-set structure to update in place.
        edges: Edge list to append exact-match relationships to.
    """
    for members in buckets.values():
        for previous, current in zip(members, members[1:], strict=False):
            union_find.union(previous, current)
            edges.append(_Edge(index_a=previous, index_b=current, similarity=1.0))


def _assemble_groups(
    *,
    items: list[HashedFile],
    media_type: MediaType,
    indices: list[int],
    union_find: UnionFind,
    edges: list[_Edge],
) -> list[DuplicateGroup]:
    """Build duplicate groups from union-find components and edges.

    Args:
        items: All parsed input files.
        media_type: Media type shared by every index in the partition.
        indices: Global indices belonging to the partition.
        union_find: Populated disjoint-set structure.
        edges: Discovered duplicate relationships.

    Returns:
        Duplicate groups with two or more members (unnumbered).
    """
    members_by_root: dict[int, list[int]] = defaultdict(list)
    for index in indices:
        members_by_root[union_find.find(index)].append(index)

    edges_by_root: dict[int, list[_Edge]] = defaultdict(list)
    for edge in edges:
        edges_by_root[union_find.find(edge.index_a)].append(edge)

    groups: list[DuplicateGroup] = []
    for root, members in members_by_root.items():
        if len(members) < 2:
            continue
        group = DuplicateGroup(group_number=1, media_type=media_type)
        for path in sorted(items[index].path for index in members):
            group.add_file(path)
        for edge in edges_by_root[root]:
            group.add_pair(
                DuplicatePair(
                    path_a=items[edge.index_a].path,
                    path_b=items[edge.index_b].path,
                    similarity=edge.similarity,
                ),
            )
        groups.append(group)
    return groups


def _similarity(*, distance: int, bit_length: int) -> float:
    """Convert a Hamming distance into a ``[0, 1]`` similarity score.

    Args:
        distance: Hamming distance between two hashes.
        bit_length: Bit length of the compared hashes.

    Returns:
        Similarity where ``1.0`` means identical and ``0.0`` fully different.
    """
    if bit_length <= 0:
        return 0.0
    return max(0.0, 1.0 - distance / bit_length)


def _sort_key(group: DuplicateGroup) -> str:
    """Return a deterministic secondary sort key for a group.

    Args:
        group: Duplicate group to derive a key from.

    Returns:
        The lexicographically smallest member path as a string.
    """
    return str(min(group.files)) if group.files else ""


__all__ = [
    "DEFAULT_HASH_DISTANCE_THRESHOLD",
    "DuplicateFinder",
    "HashedFile",
    "find_duplicates",
]
