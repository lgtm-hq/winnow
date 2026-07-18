"""Tests for the ``winnow stats`` command and its helpers."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.stats import build_stats_table, collect_directory_stats
from winnow.models.media import MediaType

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
    assert_that(result.earliest_modified).is_none()
    assert_that(result.latest_modified).is_none()


def test_build_stats_table_shows_na_for_empty(tmp_path: Path) -> None:
    """The stats table renders ``n/a`` when there are no files."""
    directory_stats = collect_directory_stats(tmp_path)

    table = build_stats_table(tmp_path, directory_stats)
    value_cells = list(table.columns[1].cells)

    assert_that(value_cells).contains("n/a")


def test_stats_command_renders_summary_table(tmp_path: Path) -> None:
    """The stats command prints an aggregate summary table."""
    _populate(tmp_path)

    result = CliRunner().invoke(main, ["stats", str(tmp_path)])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Total files")
    assert_that(result.output).contains("9 B (9 bytes)")
    assert_that(result.output).contains("2020-01-01 00:00:00")
    assert_that(result.output).contains("2023-12-31 23:59:59")


def test_stats_command_non_recursive_option(tmp_path: Path) -> None:
    """The --no-recursive flag limits stats to the top-level directory."""
    _populate(tmp_path)

    result = CliRunner().invoke(main, ["stats", str(tmp_path), "--no-recursive"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Total files")


def test_stats_command_rejects_file_argument(tmp_path: Path) -> None:
    """The stats command rejects a file argument."""
    target = tmp_path / "photo.jpg"
    target.write_text("x")

    result = CliRunner().invoke(main, ["stats", str(target)])

    assert_that(result.exit_code).is_not_equal_to(0)
