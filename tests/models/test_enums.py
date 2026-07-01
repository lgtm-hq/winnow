"""Tests for shared enumeration types."""

from __future__ import annotations

import json

from assertpy import assert_that

from winnow.models.enums import FileAction, HashAlgorithm, MediaCategory, SortOrder


def test_hash_algorithm_values_and_serialization() -> None:
    """HashAlgorithm members serialize to their auto-generated string values."""
    assert_that(HashAlgorithm.MD5.value).is_equal_to("md5")
    assert_that(HashAlgorithm.SHA256.value).is_equal_to("sha256")
    assert_that(json.dumps({"algorithm": HashAlgorithm.PHASH})).is_equal_to(
        '{"algorithm": "phash"}',
    )


def test_sort_order_members_are_strings() -> None:
    """SortOrder is a StrEnum with expected members."""
    assert_that(SortOrder.BY_NAME).is_instance_of(str)
    assert_that(SortOrder.BY_QUALITY.value).is_equal_to("by_quality")


def test_media_category_round_trip() -> None:
    """MediaCategory values round-trip through JSON."""
    payload = {"category": MediaCategory.PHOTOS}
    restored = json.loads(json.dumps(payload))
    assert_that(restored["category"]).is_equal_to("photos")
    assert_that(MediaCategory(restored["category"])).is_equal_to(MediaCategory.PHOTOS)


def test_file_action_from_string() -> None:
    """FileAction can be constructed from serialized values."""
    assert_that(FileAction("keep")).is_equal_to(FileAction.KEEP)
    assert_that(FileAction.MOVE.value).is_equal_to("move")
