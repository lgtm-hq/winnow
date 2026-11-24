"""Tests for the ``winnow stats`` command."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from assertpy import assert_that
from click.testing import CliRunner, Result
from PIL import Image

from winnow.cli import main
from winnow.cli.stats import build_stats_table
from winnow.media import inventory
from winnow.media.inventory import collect_directory_stats

if TYPE_CHECKING:
    from collections.abc import Iterator

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


# --- Content detection, walk policy and read errors (#366) -------------------


def _write_jpeg(path: Path) -> Path:
    """Write a real 8x8 JPEG to ``path`` whatever its name says.

    Args:
        path: Destination file.

    Returns:
        The written path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (120, 60, 30)).save(path, format="JPEG")
    return path


def _row(output: str, label: str) -> str:
    """Return the value cell of the table row whose metric is ``label``.

    Args:
        output: Rendered command output.
        label: Metric name in the first column.

    Returns:
        The stripped value cell.
    """
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.split("│")]
        if len(cells) >= 3 and cells[1] == label:
            return cells[2]
    pytest.fail(f"no row {label!r} in output:\n{output}")


def _invoke_stats(*args: str, config: Path) -> Result:
    """Run ``winnow stats`` with an explicit, isolated config file.

    Args:
        args: Arguments after ``stats``.
        config: Configuration file passed with ``--config``.

    Returns:
        The Click result.
    """
    return CliRunner().invoke(
        main,
        ["stats", *args, "--config", str(config)],
        env={"COLUMNS": "200"},
    )


@pytest.fixture
def empty_config(tmp_path: Path) -> Path:
    """Write an empty configuration file outside any scanned directory.

    Returns:
        Path to the file.
    """
    path = tmp_path / "config" / "winnow.toml"
    path.parent.mkdir()
    path.write_text("")
    return path


def test_stats_counts_renamed_jpeg_as_image(
    tmp_path: Path,
    empty_config: Path,
) -> None:
    """A JPEG named ``.bin`` is one image and one extension-without-type file."""
    library = tmp_path / "library"
    _write_jpeg(library / "IMG_0001.bin")

    result = _invoke_stats(str(library), config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Image files")).is_equal_to("1")
    assert_that(_row(result.output, "Unknown files")).is_equal_to("0")
    assert_that(_row(result.output, "Extension gives no type")).is_equal_to("1")
    assert_that(_row(result.output, "Extension does not match content")).is_equal_to(
        "0",
    )
    assert_that(_row(result.output, "Classified by")).is_equal_to("content")


def test_stats_counts_fixtures_by_content(empty_config: Path) -> None:
    """The fixtures count HEIC as image and the four audio samples as audio."""
    fixtures = Path(__file__).resolve().parents[1] / "media" / "fixtures"

    result = _invoke_stats(str(fixtures), "--no-recursive", config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Image files")).is_equal_to("10")
    assert_that(_row(result.output, "Audio files")).is_equal_to("4")
    assert_that(_row(result.output, "Video files")).is_equal_to("1")


def test_stats_fast_is_extension_only(tmp_path: Path, empty_config: Path) -> None:
    """``--fast`` classifies by name and says so."""
    library = tmp_path / "library"
    _write_jpeg(library / "IMG_0001.bin")
    (library / "notes.mp4").write_text("just text\n")

    result = _invoke_stats(str(library), "--fast", config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Classified by")).is_equal_to("extension only")
    assert_that(_row(result.output, "Image files")).is_equal_to("0")
    assert_that(_row(result.output, "Video files")).is_equal_to("1")
    assert_that(_row(result.output, "Unknown files")).is_equal_to("1")
    assert_that(_row(result.output, "Extension gives no type")).is_equal_to(
        "n/a (extension only)",
    )


def test_stats_reports_vanished_file(
    tmp_path: Path,
    empty_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file removed during the walk is counted unreadable; exit is 0."""
    library = tmp_path / "library"
    _write_jpeg(library / "a.jpg")
    doomed = _write_jpeg(library / "b.jpg")
    real_walk = inventory.iter_regular_files

    def vanishing_walk(*args: Any, **kwargs: Any) -> Iterator[Path]:
        for path in real_walk(*args, **kwargs):
            if path == doomed:
                path.unlink()
            yield path

    monkeypatch.setattr(inventory, "iter_regular_files", vanishing_walk)

    result = _invoke_stats(str(library), config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Unreadable files")).is_equal_to("1")
    assert_that(_row(result.output, "Total files")).is_equal_to("1")
    assert_that(result.output).contains(str(doomed))
    assert_that(_row(result.output, "Coverage")).starts_with("incomplete")


def test_stats_reports_unreadable_directory(
    tmp_path: Path,
    empty_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlistable directory is one unreadable directory, contents unknown."""
    library = tmp_path / "library"
    _write_jpeg(library / "a.jpg")
    locked = library / "locked"
    _write_jpeg(locked / "inside.jpg")
    real_scandir = os.scandir

    def fake_scandir(directory: Any) -> Any:
        if Path(directory) == locked:
            raise PermissionError(13, "Permission denied", str(directory))
        return real_scandir(directory)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    result = _invoke_stats(str(library), config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Unreadable directories")).is_equal_to("1")
    assert_that(_row(result.output, "Total files")).is_equal_to("1")
    assert_that(_row(result.output, "Coverage")).starts_with("incomplete")
    assert_that(result.output).contains(str(locked))


def test_stats_honours_exclude_patterns_from_config(tmp_path: Path) -> None:
    """Exclude patterns from the loaded configuration skip matching entries."""
    library = tmp_path / "library"
    _write_jpeg(library / "keep.jpg")
    _write_jpeg(library / "draft_1.jpg")
    config = tmp_path / "winnow.toml"
    config.write_text('[paths]\nexclude_patterns = ["draft_*"]\n')

    result = _invoke_stats(str(library), config=config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Total files")).is_equal_to("1")
    assert_that(_row(result.output, "Skipped entries")).is_equal_to(
        "1 (1 excluded by pattern)",
    )


def test_stats_skips_winnow_folders_and_symlinks(
    tmp_path: Path,
    empty_config: Path,
) -> None:
    """Winnow's own folders and symlinks are skipped under the default policy."""
    library = tmp_path / "library"
    _write_jpeg(library / "a.jpg")
    _write_jpeg(library / ".winnow-backups" / "x.jpg")
    (library / "link.jpg").symlink_to(library / "a.jpg")

    result = _invoke_stats(str(library), config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Total files")).is_equal_to("1")
    assert_that(_row(result.output, "Skipped entries")).is_equal_to(
        "2 (1 hidden or winnow folder, 1 symlink)",
    )
    assert_that(_row(result.output, "Coverage")).is_equal_to("complete")


def test_stats_reports_an_unreadable_root(
    tmp_path: Path,
    empty_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable DIRECTORY is reported by the command, not a usage error."""
    locked = tmp_path / "locked"
    _write_jpeg(locked / "inside.jpg")
    real_access = os.access
    real_scandir = os.scandir

    def fake_access(path: Any, mode: int, *args: Any, **kwargs: Any) -> bool:
        return Path(path) != locked and real_access(path, mode, *args, **kwargs)

    def fake_scandir(directory: Any) -> Any:
        if Path(directory) == locked:
            raise PermissionError(13, "Permission denied", str(directory))
        return real_scandir(directory)

    monkeypatch.setattr(os, "access", fake_access)
    monkeypatch.setattr(os, "scandir", fake_scandir)

    result = _invoke_stats(str(locked), config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Unreadable directories")).is_equal_to("1")
    assert_that(_row(result.output, "Total files")).is_equal_to("0")
    assert_that(_row(result.output, "Coverage")).starts_with("incomplete")


def test_stats_counts_name_typed_files_in_their_own_row(
    tmp_path: Path,
    empty_config: Path,
) -> None:
    """An empty ``zero.jpg`` is not counted as an image found by content."""
    library = tmp_path / "library"
    _write_jpeg(library / "real.jpg")
    (library / "zero.jpg").write_bytes(b"")

    result = _invoke_stats(str(library), config=empty_config)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(_row(result.output, "Image files")).is_equal_to("1")
    assert_that(_row(result.output, "Typed by extension only")).is_equal_to(
        "1 (1 image)",
    )
    assert_that(_row(result.output, "Total files")).is_equal_to("2")
