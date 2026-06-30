"""Tests for media domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from assertpy import assert_that
from pydantic import ValidationError

from winnow.models.media import MediaFile, MediaMetadata, MediaType


def test_media_file_validation_and_json_round_trip(tmp_path: Path) -> None:
    """MediaFile validates fields and round-trips through JSON."""
    media_path = tmp_path / "photo.jpg"
    created = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    media_file = MediaFile(
        path=media_path,
        media_type=MediaType.IMAGE,
        creation_date=created,
        extension=".jpg",
        size_bytes=1024,
        metadata=MediaMetadata(width=1920, height=1080, image_format="JPEG"),
    )

    restored = MediaFile.model_validate_json(media_file.model_dump_json())

    assert_that(restored.path).is_equal_to(media_path)
    assert_that(restored.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(restored.metadata).is_not_none()
    metadata = restored.metadata
    if metadata is None:
        pytest.fail("expected metadata to be present after round-trip")
    assert_that(metadata.width).is_equal_to(1920)


def test_media_file_rejects_negative_size() -> None:
    """MediaFile rejects negative size_bytes values."""
    with pytest.raises(ValidationError):
        MediaFile(
            path=Path("/photos/example.jpg"),
            media_type=MediaType.IMAGE,
            creation_date=datetime(2024, 1, 1, tzinfo=UTC),
            extension=".jpg",
            size_bytes=-1,
        )


def test_media_type_serialization() -> None:
    """MediaType serializes to lowercase auto values."""
    assert_that(MediaType.VIDEO.value).is_equal_to("video")
    assert_that(MediaType.AUDIO).is_instance_of(str)
