"""Tests for configuration domain models."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that
from pydantic import ValidationError

from winnow.models.config import CacheSettings, PathSettings, WinnowConfig
from winnow.models.enums import HashAlgorithm, MediaCategory, SortOrder, SymlinkPolicy


def test_winnow_config_defaults() -> None:
    """WinnowConfig exposes sensible defaults for future YAML loading."""
    config = WinnowConfig()

    assert_that(config.hash_algorithm).is_equal_to(HashAlgorithm.SHA256)
    assert_that(config.sort_order).is_equal_to(SortOrder.BY_QUALITY)
    assert_that(config.media_categories).contains(MediaCategory.ALL)
    assert_that(config.dry_run).is_true()
    assert_that(config.symlink_policy).is_equal_to(SymlinkPolicy.SKIP)
    assert_that(config.workers).is_equal_to(1)
    assert_that(config.cache).is_instance_of(CacheSettings)
    assert_that(config.paths).is_instance_of(PathSettings)


def test_winnow_config_validation() -> None:
    """WinnowConfig validates similarity bounds and source directories."""
    config = WinnowConfig(
        source_dirs=[Path("/media/photos"), Path("/media/videos")],
        min_similarity=0.8,
        dry_run=False,
    )

    restored = WinnowConfig.model_validate_json(config.model_dump_json())
    assert_that(restored.source_dirs[0]).is_equal_to(Path("/media/photos"))


def test_winnow_config_rejects_invalid_similarity() -> None:
    """WinnowConfig rejects min_similarity outside [0, 1]."""
    with pytest.raises(ValidationError):
        WinnowConfig(min_similarity=1.5)


def test_winnow_config_rejects_invalid_workers() -> None:
    """WinnowConfig rejects worker counts below one."""
    with pytest.raises(ValidationError):
        WinnowConfig(workers=0)
