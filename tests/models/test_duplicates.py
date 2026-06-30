"""Tests for duplicate detection domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from assertpy import assert_that
from pydantic import ValidationError

from winnow.models.duplicates import DuplicateGroup, DuplicatePair, QualityScore
from winnow.models.media import MediaType


def test_duplicate_pair_validation() -> None:
    """DuplicatePair validates distinct paths and similarity bounds."""
    pair = DuplicatePair(
        path_a=Path("/photos/a.jpg"),
        path_b=Path("/photos/b.jpg"),
        similarity=0.98,
    )
    assert_that(pair.similarity).is_equal_to(0.98)


def test_duplicate_pair_rejects_identical_paths() -> None:
    """DuplicatePair rejects pairs where both paths are identical."""
    same_path = Path("/photos/a.jpg")
    with pytest.raises(ValidationError, match="Duplicate pair paths must differ"):
        DuplicatePair(path_a=same_path, path_b=same_path)


def test_duplicate_pair_rejects_identical_paths_after_assignment() -> None:
    """DuplicatePair re-validates when either path is assigned after construction."""
    path_a = Path("/photos/a.jpg")
    path_b = Path("/photos/b.jpg")

    pair = DuplicatePair(path_a=path_a, path_b=path_b)
    with pytest.raises(ValidationError, match="Duplicate pair paths must differ"):
        pair.path_a = path_b

    pair = DuplicatePair(path_a=path_a, path_b=path_b)
    with pytest.raises(ValidationError, match="Duplicate pair paths must differ"):
        pair.path_b = path_a


def test_duplicate_group_add_file_and_to_dict() -> None:
    """DuplicateGroup tracks files, depth, and serializes to dict."""
    group = DuplicateGroup(group_number=1, media_type=MediaType.IMAGE)
    group.add_file(Path("/photos/a.jpg"), depth=2)
    group.add_file(Path("/photos/b.jpg"), depth=4)

    payload = group.to_dict()

    assert_that(group.max_depth).is_equal_to(4)
    assert_that(payload["files"]).contains("/photos/a.jpg", "/photos/b.jpg")


def test_duplicate_group_add_pair_registers_both_paths() -> None:
    """add_pair appends the pair and registers both file paths."""
    group = DuplicateGroup(group_number=2, media_type=MediaType.VIDEO)
    pair = DuplicatePair(
        path_a=Path("/videos/a.mp4"),
        path_b=Path("/videos/b.mp4"),
    )
    group.add_pair(pair)

    assert_that(group.pairs).is_length(1)
    assert_that(group.files).is_length(2)


def test_quality_score_validation() -> None:
    """QualityScore validates non-negative numeric fields."""
    score = QualityScore(
        composite_score=0.91,
        resolution=2073600,
        quality_metric=0.88,
        file_size=2048,
        creation_date=datetime(2024, 3, 1, tzinfo=UTC),
        width=1920,
        height=1080,
    )
    assert_that(score.composite_score).is_equal_to(0.91)
