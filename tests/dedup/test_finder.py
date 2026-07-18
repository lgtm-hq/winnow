"""Tests for the perceptual-hash duplicate finder."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.dedup.finder import (
    DEFAULT_HASH_DISTANCE_THRESHOLD,
    DuplicateFinder,
    HashedFile,
    find_duplicates,
)
from winnow.exceptions import DuplicateError
from winnow.models.config import WinnowConfig
from winnow.models.media import MediaType


def _image(path: str, perceptual_hash: str) -> HashedFile:
    """Build an image ``HashedFile`` for tests.

    Args:
        path: File path string.
        perceptual_hash: Perceptual hash string.

    Returns:
        Image hashed-file input.
    """
    return HashedFile(
        path=Path(path),
        perceptual_hash=perceptual_hash,
        media_type=MediaType.IMAGE,
    )


def test_exact_duplicates_group_with_full_similarity() -> None:
    """Files sharing an identical hash form one group with similarity 1.0."""
    groups = find_duplicates(
        [
            _image("/a.jpg", "ff00"),
            _image("/b.jpg", "ff00"),
            _image("/c.jpg", "0000"),
        ],
        threshold=0,
    )

    assert_that(groups).is_length(1)
    group = groups[0]
    assert_that(group.files).contains_only(Path("/a.jpg"), Path("/b.jpg"))
    assert_that(group.pairs).is_length(1)
    assert_that(group.pairs[0].similarity).is_equal_to(1.0)


def test_near_duplicates_within_threshold_group_together() -> None:
    """Hashes within the threshold group and expose a computed similarity."""
    groups = find_duplicates(
        [
            _image("/a.jpg", "ff00"),
            _image("/b.jpg", "ff01"),
        ],
        threshold=2,
    )

    assert_that(groups).is_length(1)
    group = groups[0]
    assert_that(group.files).contains_only(Path("/a.jpg"), Path("/b.jpg"))
    assert_that(group.pairs[0].similarity).is_close_to(1.0 - 1.0 / 16.0, 1e-9)


def test_transitive_closure_merges_indirect_matches() -> None:
    """Chained near-matches collapse into a single group via union-find."""
    groups = find_duplicates(
        [
            _image("/a.jpg", "f0"),
            _image("/b.jpg", "f1"),
            _image("/c.jpg", "f3"),
        ],
        threshold=1,
    )

    assert_that(groups).is_length(1)
    assert_that(groups[0].files).contains_only(
        Path("/a.jpg"),
        Path("/b.jpg"),
        Path("/c.jpg"),
    )


def test_files_beyond_threshold_are_not_grouped() -> None:
    """Distant hashes never join a group and singletons are dropped."""
    groups = find_duplicates(
        [
            _image("/a.jpg", "ff00"),
            _image("/b.jpg", "00ff"),
        ],
        threshold=4,
    )

    assert_that(groups).is_empty()


def test_threshold_boundary_is_inclusive() -> None:
    """A distance equal to the threshold counts as a duplicate."""
    grouped = find_duplicates(
        [_image("/a.jpg", "f0"), _image("/b.jpg", "f3")],
        threshold=2,
    )
    not_grouped = find_duplicates(
        [_image("/a.jpg", "f0"), _image("/b.jpg", "f7")],
        threshold=2,
    )

    assert_that(grouped).is_length(1)
    assert_that(not_grouped).is_empty()


def test_groups_are_sorted_by_member_count_descending() -> None:
    """Larger groups sort first and receive lower group numbers."""
    groups = find_duplicates(
        [
            _image("/big1.jpg", "00"),
            _image("/big2.jpg", "00"),
            _image("/big3.jpg", "00"),
            _image("/small1.jpg", "ff"),
            _image("/small2.jpg", "ff"),
        ],
        threshold=0,
    )

    assert_that(groups).is_length(2)
    assert_that(groups[0].files).is_length(3)
    assert_that(groups[0].group_number).is_equal_to(1)
    assert_that(groups[1].files).is_length(2)
    assert_that(groups[1].group_number).is_equal_to(2)


def test_different_media_types_do_not_group() -> None:
    """Identical hashes across media types stay in separate partitions."""
    groups = find_duplicates(
        [
            HashedFile(Path("/a.jpg"), "ff00", MediaType.IMAGE),
            HashedFile(Path("/a.mp4"), "ff00", MediaType.VIDEO),
        ],
        threshold=0,
    )

    assert_that(groups).is_empty()


def test_different_bit_lengths_are_not_compared() -> None:
    """Hashes of different widths never match, avoiding meaningless distances."""
    groups = find_duplicates(
        [
            _image("/short.jpg", "00"),
            _image("/long.jpg", "0000"),
        ],
        threshold=64,
    )

    assert_that(groups).is_empty()


def test_group_media_type_matches_partition() -> None:
    """A group carries the media type of its members."""
    groups = find_duplicates(
        [
            HashedFile(Path("/a.mp4"), "ff00", MediaType.VIDEO),
            HashedFile(Path("/b.mp4"), "ff00", MediaType.VIDEO),
        ],
        threshold=0,
    )

    assert_that(groups[0].media_type).is_equal_to(MediaType.VIDEO)


def test_from_config_uses_configured_threshold() -> None:
    """The finder reads its threshold from a provided config."""
    config = WinnowConfig(perceptual_hash_distance_threshold=1)
    finder = DuplicateFinder.from_config(config)

    assert_that(finder.threshold).is_equal_to(1)
    groups = finder.find([_image("/a.jpg", "f0"), _image("/b.jpg", "f3")])
    assert_that(groups).is_empty()


def test_from_config_defaults_without_config() -> None:
    """A missing config falls back to the module default threshold."""
    finder = DuplicateFinder.from_config()

    assert_that(finder.threshold).is_equal_to(DEFAULT_HASH_DISTANCE_THRESHOLD)


def test_duplicate_path_input_is_rejected() -> None:
    """Providing the same path twice raises a duplicate error."""
    with pytest.raises(DuplicateError, match="duplicate path"):
        find_duplicates(
            [_image("/a.jpg", "00"), _image("/a.jpg", "01")],
            threshold=1,
        )


def test_invalid_hash_reports_offending_path() -> None:
    """An unparseable hash raises an error carrying the file path."""
    with pytest.raises(DuplicateError) as error:
        find_duplicates([_image("/bad.jpg", "xyz")])

    assert_that(str(error.value.context.file_path)).is_equal_to("/bad.jpg")


def test_negative_threshold_is_rejected() -> None:
    """A negative threshold is invalid."""
    with pytest.raises(DuplicateError, match="non-negative"):
        DuplicateFinder(threshold=-1)


def test_empty_input_returns_no_groups() -> None:
    """An empty input yields no duplicate groups."""
    assert_that(find_duplicates([])).is_empty()


def test_scales_to_many_files_without_explosion() -> None:
    """Union-find grouping handles a few thousand files efficiently."""
    files = [_image(f"/dup_{index}.jpg", "ff00") for index in range(1_000)]
    files.extend(_image(f"/uniq_{index}.jpg", "0000") for index in range(1_000))

    groups = find_duplicates(files, threshold=0)

    assert_that(groups).is_length(2)
    assert_that(groups[0].files).is_length(1_000)
