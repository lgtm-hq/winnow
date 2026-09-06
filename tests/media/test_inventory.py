"""Tests for directory inventory and file inspection in :mod:`winnow.media`."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from assertpy import assert_that

from winnow.media.inventory import (
    collect_directory_stats,
    inspect_file,
    iter_regular_files,
)
from winnow.models.media import MediaType

if TYPE_CHECKING:
    from pathlib import Path

_EARLY = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp()
_LATE = datetime(2023, 12, 31, 23, 59, 59, tzinfo=UTC).timestamp()


def _populate(root: Path) -> None:
    """Create a mixed-media directory tree with known modification times.

    Args:
        root: Directory to populate.
    """
    (root / "sub").mkdir()
    image = root / "photo.jpg"
    video = root / "sub" / "clip.mp4"
    audio = root / "song.mp3"
    unknown = root / "notes.txt"
    for path, content in (
        (image, "aa"),
        (video, "bbbb"),
        (audio, "cc"),
        (unknown, "d"),
    ):
        path.write_text(content)
    os.utime(image, (_EARLY, _EARLY))
    os.utime(video, (_LATE, _LATE))
    os.utime(audio, (_EARLY, _EARLY))
    os.utime(unknown, (_EARLY, _EARLY))


def test_iter_regular_files_skips_directories_and_symlinks(tmp_path: Path) -> None:
    """Only regular files are yielded; directories and symlinks are skipped."""
    _populate(tmp_path)
    (tmp_path / "link.jpg").symlink_to(tmp_path / "photo.jpg")

    result = list(iter_regular_files(tmp_path))

    assert_that(result).contains_only(
        tmp_path / "photo.jpg",
        tmp_path / "sub" / "clip.mp4",
        tmp_path / "song.mp3",
        tmp_path / "notes.txt",
    )


def test_iter_regular_files_non_recursive_stays_top_level(tmp_path: Path) -> None:
    """Non-recursive iteration ignores files in subdirectories."""
    _populate(tmp_path)

    result = list(iter_regular_files(tmp_path, recursive=False))

    assert_that(result).does_not_contain(tmp_path / "sub" / "clip.mp4")
    assert_that(result).is_length(3)


def test_collect_directory_stats_aggregates_recursively(tmp_path: Path) -> None:
    """Recursive stats count every file and classify by media type."""
    _populate(tmp_path)

    result = collect_directory_stats(tmp_path, recursive=True)

    assert_that(result.total_files).is_equal_to(4)
    assert_that(result.total_bytes).is_equal_to(9)
    assert_that(result.counts_by_type.get(MediaType.IMAGE)).is_equal_to(1)
    assert_that(result.counts_by_type.get(MediaType.VIDEO)).is_equal_to(1)
    assert_that(result.counts_by_type.get(MediaType.AUDIO)).is_equal_to(1)
    assert_that(result.unknown_count).is_equal_to(1)


def test_collect_directory_stats_tracks_modification_span(tmp_path: Path) -> None:
    """Stats capture the earliest and latest modification times."""
    _populate(tmp_path)

    result = collect_directory_stats(tmp_path, recursive=True)

    assert_that(result.earliest_modified).is_equal_to(
        datetime.fromtimestamp(_EARLY, tz=UTC),
    )
    assert_that(result.latest_modified).is_equal_to(
        datetime.fromtimestamp(_LATE, tz=UTC),
    )


def test_collect_directory_stats_respects_non_recursive(tmp_path: Path) -> None:
    """Non-recursive stats ignore files in subdirectories."""
    _populate(tmp_path)

    result = collect_directory_stats(tmp_path, recursive=False)

    assert_that(result.total_files).is_equal_to(3)
    assert_that(result.counts_by_type.get(MediaType.VIDEO)).is_none()


def test_collect_directory_stats_handles_empty_directory(tmp_path: Path) -> None:
    """An empty directory yields zeroed counts and no date span."""
    result = collect_directory_stats(tmp_path)

    assert_that(result.total_files).is_equal_to(0)
    assert_that(result.total_bytes).is_equal_to(0)
    assert_that(result.unknown_count).is_equal_to(0)
    assert_that(result.counts_by_type).is_empty()
    assert_that(result.earliest_modified).is_none()
    assert_that(result.latest_modified).is_none()


def test_inspect_file_reads_image_metadata(fixtures_dir: Path) -> None:
    """A decodable image reports its media type and structural metadata."""
    file_info = inspect_file(fixtures_dir / "sample.png")

    assert_that(file_info.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(file_info.extension).is_equal_to("png")
    assert_that(file_info.size_bytes).is_greater_than(0)
    metadata = file_info.metadata
    assert_that(metadata).is_not_none()
    if metadata is None:
        pytest.fail("expected image metadata for sample.png")
    assert_that(metadata.width).is_greater_than(0)
    assert_that(metadata.image_format).is_equal_to("PNG")


def test_inspect_file_reports_timestamps_in_utc(tmp_path: Path) -> None:
    """Modification time is read from ``stat`` and expressed in UTC."""
    target = tmp_path / "notes.txt"
    target.write_text("payload")
    os.utime(target, (_EARLY, _EARLY))

    file_info = inspect_file(target)

    assert_that(file_info.modified).is_equal_to(datetime.fromtimestamp(_EARLY, tz=UTC))
    assert_that(file_info.modified.tzinfo).is_equal_to(UTC)
    assert_that(file_info.size_bytes).is_equal_to(7)


def test_inspect_file_text_file_has_no_media_type(tmp_path: Path) -> None:
    """An unrecognized extension yields no media type and no metadata."""
    target = tmp_path / "notes.txt"
    target.write_text("payload")

    file_info = inspect_file(target)

    assert_that(file_info.media_type).is_none()
    assert_that(file_info.extension).is_equal_to("txt")
    assert_that(file_info.metadata).is_none()


def test_inspect_file_non_image_media_has_no_metadata(tmp_path: Path) -> None:
    """Video and audio files are classified but carry no metadata yet."""
    target = tmp_path / "clip.mp4"
    target.write_text("0123456789")

    file_info = inspect_file(target)

    assert_that(file_info.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(file_info.size_bytes).is_equal_to(10)
    assert_that(file_info.metadata).is_none()


def test_inspect_file_corrupt_image_degrades_to_none(fixtures_dir: Path) -> None:
    """An undecodable image still classifies as an image with no metadata."""
    file_info = inspect_file(fixtures_dir / "corrupt.jpg")

    assert_that(file_info.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(file_info.metadata).is_none()
