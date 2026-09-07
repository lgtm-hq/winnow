"""Tests for the ``winnow info`` command."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.info import build_info_table
from winnow.media.inventory import FileInfo
from winnow.models.media import MediaMetadata, MediaType

_FIXED_MTIME = datetime(2022, 6, 1, 12, 0, 0, tzinfo=UTC).timestamp()


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


def _file_info(metadata: MediaMetadata | None) -> FileInfo:
    """Build a ``FileInfo`` for an image with the given metadata.

    Args:
        metadata: Structural metadata to attach, or ``None``.

    Returns:
        A populated ``FileInfo`` record.
    """
    moment = datetime(2022, 6, 1, 12, 0, 0, tzinfo=UTC)
    return FileInfo(
        path=Path("pic.png"),
        media_type=MediaType.IMAGE,
        extension="png",
        size_bytes=2048,
        modified=moment,
        accessed=moment,
        changed=moment,
        metadata=metadata,
    )


def test_build_info_table_includes_image_rows() -> None:
    """The info table adds image rows when metadata is present."""
    table = build_info_table(
        _file_info(
            MediaMetadata(width=32, height=16, image_format="PNG", color_mode="RGBA"),
        ),
    )
    field_cells = list(table.columns[0].cells)
    value_cells = list(table.columns[1].cells)

    assert_that(field_cells).contains("Image format", "Color mode", "Dimensions")
    assert_that(value_cells).contains("PNG", "RGBA", "32x16")


def test_build_info_table_omits_image_rows_without_metadata() -> None:
    """The info table has no image rows when metadata is ``None``."""
    table = build_info_table(_file_info(None))
    field_cells = list(table.columns[0].cells)

    assert_that(field_cells).does_not_contain("Image format", "Dimensions")


def test_build_info_table_defaults_missing_format_and_dimensions() -> None:
    """Partial metadata falls back to ``unknown`` and skips dimensions."""
    table = build_info_table(_file_info(MediaMetadata(width=32)))
    field_cells = list(table.columns[0].cells)
    value_cells = list(table.columns[1].cells)

    assert_that(value_cells).contains("unknown")
    assert_that(field_cells).does_not_contain("Dimensions")


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


def test_info_command_renders_image_rows_for_real_image(tmp_path: Path) -> None:
    """The info command shows dimensions for a decodable image."""
    fixture = Path(__file__).resolve().parents[1] / "media" / "fixtures" / "sample.png"

    result = CliRunner().invoke(main, ["info", str(fixture)])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Image format")
    assert_that(result.output).contains("Dimensions")


def test_info_command_rejects_directory(tmp_path: Path) -> None:
    """The info command rejects a directory argument."""
    result = CliRunner().invoke(main, ["info", str(tmp_path)])

    assert_that(result.exit_code).is_not_equal_to(0)
