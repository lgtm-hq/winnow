"""Tests for configuration domain models."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that
from pydantic import ValidationError

from winnow.models.config import (
    CacheSettings,
    PathSettings,
    RoutingSettings,
    WinnowConfig,
)
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
    assert_that(config.cache.directory).is_equal_to(Path.home() / ".cache" / "winnow")
    assert_that(config.paths).is_instance_of(PathSettings)
    assert_that(config.routing).is_instance_of(RoutingSettings)


def test_cache_settings_fields_are_enabled_and_directory() -> None:
    """CacheSettings exposes only the fields the cache implementation reads."""
    assert_that(set(CacheSettings.model_fields)).is_equal_to({"enabled", "directory"})


def test_cache_settings_rejects_removed_ttl_field() -> None:
    """Stale ``ttl_seconds``/``max_size_mb`` keys fail validation loudly."""
    with pytest.raises(ValidationError):
        CacheSettings.model_validate({"ttl_seconds": 1})
    with pytest.raises(ValidationError):
        CacheSettings.model_validate({"max_size_mb": 1})


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


def test_winnow_config_rejects_conflicting_symlink_settings() -> None:
    """WinnowConfig rejects contradictory symlink flags and policies."""
    with pytest.raises(ValidationError):
        WinnowConfig(follow_symlinks=True, symlink_policy=SymlinkPolicy.ERROR)

    with pytest.raises(ValidationError):
        WinnowConfig(follow_symlinks=False, symlink_policy=SymlinkPolicy.FOLLOW)


def test_routing_settings_defaults() -> None:
    """RoutingSettings exposes the documented folder names and thresholds."""
    settings = RoutingSettings()

    assert_that(settings.enabled).is_true()
    assert_that(settings.screenshots).is_equal_to("Screenshots")
    assert_that(settings.graphics).is_equal_to("Graphics")
    assert_that(settings.live_photos).is_equal_to("LivePhotos")
    assert_that(settings.review).is_equal_to("Review")
    assert_that(settings.duplicates).is_equal_to("Duplicates")
    assert_that(settings.min_confidence).is_equal_to(0.75)
    assert_that(settings.keep_dated_layout).is_true()


def test_routing_settings_accepts_renamed_folder() -> None:
    """RoutingSettings accepts a distinct custom folder name."""
    settings = RoutingSettings(graphics="Memes")

    assert_that(settings.graphics).is_equal_to("Memes")


@pytest.mark.parametrize(
    ("overrides", "message_fragment"),
    [
        ({"screenshots": "Shots/2024"}, "routing.screenshots"),
        ({"review": ""}, "routing.review"),
        ({"graphics": " Graphics"}, "routing.graphics"),
        ({"live_photos": "a\\b"}, "routing.live_photos"),
        ({"duplicates": ".."}, "routing.duplicates"),
        ({"review": "Re\x00view"}, "routing.review"),
        ({"screenshots": "2024"}, "routing.screenshots"),
        ({"graphics": "screenshots"}, "routing.graphics"),
    ],
    ids=[
        "slash",
        "empty",
        "padded",
        "backslash",
        "dot_dot",
        "nul",
        "year_like",
        "case_insensitive_duplicate",
    ],
)
def test_routing_settings_rejects_unsafe_folder_names(
    overrides: dict[str, str],
    message_fragment: str,
) -> None:
    """RoutingSettings rejects unsafe or duplicated folder names by field."""
    with pytest.raises(ValidationError) as excinfo:
        RoutingSettings.model_validate(overrides)

    assert_that(str(excinfo.value)).contains(message_fragment)


def test_routing_settings_rejects_out_of_range_confidence() -> None:
    """RoutingSettings rejects min_confidence outside [0, 1]."""
    with pytest.raises(ValidationError):
        RoutingSettings(min_confidence=1.5)


def test_routing_settings_rejects_unknown_keys() -> None:
    """RoutingSettings forbids unknown fields."""
    with pytest.raises(ValidationError):
        RoutingSettings.model_validate({"memes": "Memes"})
