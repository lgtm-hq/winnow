"""Tests for the ``winnow info`` command."""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that
from click.testing import CliRunner, Result
from PIL import Image

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.cli.info import build_info_table
from winnow.media.inventory import FileInfo, inspect_file
from winnow.models.media import MediaMetadata, MediaType

info_module = importlib.import_module("winnow.cli.info")
"""The ``winnow.cli.info`` module; ``winnow.cli.info`` the attribute is the command."""

_FIXED_MTIME = datetime(2022, 6, 1, 12, 0, 0, tzinfo=UTC).timestamp()


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
    target = _write_jpeg(tmp_path / "pic.jpg")
    os.utime(target, (_FIXED_MTIME, _FIXED_MTIME))
    size = target.stat().st_size

    result = _invoke_info(str(target))

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Media file info")
    assert_that(result.output).contains("pic.jpg")
    assert_that(_row(result.output, "Media type")).is_equal_to("image")
    assert_that(_row(result.output, "Extension check")).is_equal_to(
        "matches content",
    )
    assert_that(result.output).contains("2022-06-01 12:00:00")
    assert_that(result.output).contains(f"({size} bytes)")


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


# --- Content detection and read errors (#366) --------------------------------


def _write_jpeg(path: Path) -> Path:
    """Write a real 8x6 JPEG to ``path`` whatever its name says.

    Args:
        path: Destination file.

    Returns:
        The written path.
    """
    Image.new("RGB", (8, 6), (120, 60, 30)).save(path, format="JPEG")
    return path


def _row(output: str, label: str) -> str:
    """Return the value cell of the table row whose field is ``label``.

    Args:
        output: Rendered command output.
        label: Field name in the first column.

    Returns:
        The stripped value cell.
    """
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.split("│")]
        if len(cells) >= 3 and cells[1] == label:
            return cells[2]
    pytest.fail(f"no row {label!r} in output:\n{output}")


def _invoke_info(*args: str) -> Result:
    """Run ``winnow info`` with a wide terminal.

    Args:
        args: Arguments after ``info``.

    Returns:
        The Click result.
    """
    return CliRunner().invoke(main, ["info", *args], env={"COLUMNS": "200"})


def test_info_reports_extension_without_type(tmp_path: Path) -> None:
    """A JPEG named ``.bin`` is an image whose extension gives no type."""
    target = _write_jpeg(tmp_path / "IMG_0001.bin")

    result = _invoke_info(str(target))

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Media type")).is_equal_to("image")
    assert_that(_row(result.output, "Dimensions")).is_equal_to("8x6")
    assert_that(_row(result.output, "Extension check")).is_equal_to(
        "extension gives no type",
    )


def test_info_reports_extension_mismatch(tmp_path: Path) -> None:
    """A JPEG named ``.mp3`` reports both the content and the extension type."""
    target = _write_jpeg(tmp_path / "photo.mp3")

    result = _invoke_info(str(target))

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Media type")).is_equal_to("image")
    assert_that(_row(result.output, "Extension check")).is_equal_to(
        "mismatch: content says image, extension says audio",
    )


def test_info_reports_text_named_mp4_as_unknown(tmp_path: Path) -> None:
    """A text file named ``.mp4`` is unknown; the extension's claim is shown."""
    target = tmp_path / "notes.mp4"
    target.write_text("just text\n")

    result = _invoke_info(str(target))

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Media type")).is_equal_to("unknown")
    assert_that(_row(result.output, "Extension check")).is_equal_to(
        "content gives no type; extension says video",
    )


def test_info_fast_is_extension_only(tmp_path: Path) -> None:
    """``--fast`` types by name and labels the answer."""
    target = _write_jpeg(tmp_path / "IMG_0001.bin")

    result = _invoke_info(str(target), "--fast")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Media type")).is_equal_to(
        "unknown (extension only)",
    )
    assert_that(_row(result.output, "Extension check")).is_equal_to(
        "not checked (extension only)",
    )


def test_info_reports_vanished_file_as_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file removed after argument parsing exits 1 with a message, no traceback."""
    target = _write_jpeg(tmp_path / "gone.jpg")
    real_inspect = inspect_file

    def remove_then_inspect(path: Path, **kwargs: Any) -> FileInfo:
        path.unlink()
        return real_inspect(path, **kwargs)

    monkeypatch.setattr(info_module, "inspect_file", remove_then_inspect)

    result = CliRunner().invoke(main, ["info", str(target)])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("cannot read file")
    assert_that(result.stderr).contains("inspect_file")
    assert_that(result.output).does_not_contain("Traceback")


def test_build_info_table_renders_video_duration_and_codec() -> None:
    """Video metadata adds duration and codec rows and no image rows."""
    moment = datetime(2022, 6, 1, 12, 0, 0, tzinfo=UTC)
    file_info = FileInfo(
        path=Path("clip.mp4"),
        media_type=MediaType.VIDEO,
        extension="mp4",
        size_bytes=2048,
        modified=moment,
        accessed=moment,
        changed=moment,
        metadata=MediaMetadata(duration_seconds=2.5, codec="h264"),
    )

    table = build_info_table(file_info)
    field_cells = list(table.columns[0].cells)
    value_cells = list(table.columns[1].cells)

    assert_that(field_cells).contains("Duration", "Codec")
    assert_that(field_cells).does_not_contain("Image format")
    assert_that(value_cells).contains("2.500 s", "h264")


def test_build_info_table_skips_missing_audio_fields() -> None:
    """Audio metadata without duration or codec adds neither row."""
    moment = datetime(2022, 6, 1, 12, 0, 0, tzinfo=UTC)
    file_info = FileInfo(
        path=Path("song.mp3"),
        media_type=MediaType.AUDIO,
        extension="mp3",
        size_bytes=2048,
        modified=moment,
        accessed=moment,
        changed=moment,
        metadata=MediaMetadata(),
    )

    field_cells = list(build_info_table(file_info).columns[0].cells)

    assert_that(field_cells).does_not_contain("Duration", "Codec")


def test_info_reports_unknown_content_and_extension(tmp_path: Path) -> None:
    """A text file with an untyped name says neither gives a type."""
    target = tmp_path / "notes.txt"
    target.write_text("just text\n")

    result = _invoke_info(str(target))

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Extension check")).is_equal_to(
        "content gives no type; extension gives no type",
    )
