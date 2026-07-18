"""Tests for the ``winnow info`` command and its helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.info import (
    ImageSummary,
    build_info_table,
    collect_file_info,
    read_image_summary,
    summarize_image,
)
from winnow.models.media import MediaType

_FIXED_MTIME = datetime(2022, 6, 1, 12, 0, 0, tzinfo=UTC).timestamp()


@dataclass(frozen=True, slots=True)
class _FakeImage:
    """Minimal Pillow-like image used to exercise summary extraction."""

    format: str | None
    mode: str
    width: int
    height: int


def _make_file(path: Path, *, content: str = "payload") -> Path:
    """Create a file with a fixed modification time.

    Args:
        path: File path to create.
        content: Text content to write.

    Returns:
        The created file path.
    """
    path.write_text(content)
    os.utime(path, (_FIXED_MTIME, _FIXED_MTIME))
    return path


def test_collect_file_info_classifies_media_and_size(tmp_path: Path) -> None:
    """File info reports media type, extension, and byte size."""
    target = _make_file(tmp_path / "clip.mp4", content="0123456789")

    file_info = collect_file_info(target)

    assert_that(file_info.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(file_info.extension).is_equal_to("mp4")
    assert_that(file_info.size_bytes).is_equal_to(10)
    assert_that(file_info.image).is_none()


def test_collect_file_info_marks_unknown_extension(tmp_path: Path) -> None:
    """An unrecognized extension yields a ``None`` media type."""
    target = _make_file(tmp_path / "notes.xyz")

    file_info = collect_file_info(target)

    assert_that(file_info.media_type).is_none()


def test_summarize_image_reads_dimensions_and_format() -> None:
    """Image summary extraction reads format, mode, and dimensions."""
    summary = summarize_image(
        _FakeImage(format="JPEG", mode="RGB", width=640, height=480),
    )

    assert_that(summary).is_equal_to(
        ImageSummary(image_format="JPEG", mode="RGB", width=640, height=480),
    )


def test_summarize_image_defaults_missing_format() -> None:
    """A missing container format falls back to ``unknown``."""
    summary = summarize_image(
        _FakeImage(format=None, mode="L", width=1, height=1),
    )

    assert_that(summary.image_format).is_equal_to("unknown")


def test_read_image_summary_returns_none_without_pillow(tmp_path: Path) -> None:
    """Without Pillow installed, image reading degrades to ``None``."""
    target = _make_file(tmp_path / "pic.png")

    assert_that(read_image_summary(target)).is_none()


def test_build_info_table_includes_image_rows() -> None:
    """The info table adds image rows when an image summary is present."""
    from winnow.cli.info import FileInfo

    moment = datetime(2022, 6, 1, 12, 0, 0, tzinfo=UTC)
    file_info = FileInfo(
        path=Path("pic.png"),
        media_type=MediaType.IMAGE,
        extension="png",
        size_bytes=2048,
        modified=moment,
        accessed=moment,
        changed=moment,
        image=ImageSummary(image_format="PNG", mode="RGBA", width=32, height=16),
    )

    table = build_info_table(file_info)
    column_cells = list(table.columns[0].cells)

    assert_that(column_cells).contains("Image format", "Dimensions")


def test_info_command_renders_metadata_table(tmp_path: Path) -> None:
    """The info command prints a metadata table for a media file."""
    target = _make_file(tmp_path / "pic.jpg", content="abc")

    result = CliRunner().invoke(main, ["info", str(target)])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Media file info")
    assert_that(result.output).contains("pic.jpg")
    assert_that(result.output).contains("image")
    assert_that(result.output).contains("2022-06-01 12:00:00")
    assert_that(result.output).contains("3 B (3 bytes)")


def test_info_command_rejects_directory(tmp_path: Path) -> None:
    """The info command rejects a directory argument."""
    result = CliRunner().invoke(main, ["info", str(tmp_path)])

    assert_that(result.exit_code).is_not_equal_to(0)
