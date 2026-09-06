"""Tests for the ``winnow clean`` command."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.clean import clean
from winnow.fs.errors import FileSystemOperationError


def _make_tree(root: Path) -> None:
    """Create a sample directory tree with mixed empty and populated dirs.

    Args:
        root: Directory to populate.
    """
    (root / "photos" / "2020").mkdir(parents=True)
    (root / "photos" / "2020" / "pic.jpg").write_text("data")
    (root / "empty" / "nested" / "leaf").mkdir(parents=True)
    (root / "empty2").mkdir()


def test_clean_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    """A dry run lists candidates and leaves the tree intact."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("4 empty directories to remove (dry run)")
    assert_that((tmp_path / "empty").exists()).is_true()


def test_clean_dry_run_prints_markup_like_names_verbatim(tmp_path: Path) -> None:
    """Paths that look like Rich markup are printed literally."""
    # ``/`` splits the name into two nested directories, so the leaf path
    # ends in the literal ``[bold]x[/bold]``.
    (tmp_path / "[bold]x[/bold]").mkdir(parents=True)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("[bold]x[/bold]")


def test_clean_removes_directories_with_yes_flag(tmp_path: Path) -> None:
    """The --yes flag skips confirmation and removes empty directories."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--yes"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 4 empty directories.")
    assert_that((tmp_path / "empty").exists()).is_false()


def test_clean_prompts_and_aborts_on_decline(tmp_path: Path) -> None:
    """Declining the confirmation prompt preserves all directories."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path)], input="n\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Aborted.")
    assert_that((tmp_path / "empty").exists()).is_true()


def test_clean_confirms_and_removes_on_accept(tmp_path: Path) -> None:
    """Accepting the confirmation prompt removes empty directories."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path)], input="y\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 4 empty directories.")
    assert_that((tmp_path / "empty").exists()).is_false()


def test_clean_reports_when_no_empty_directories(tmp_path: Path) -> None:
    """A tree with no empty directories reports nothing to do."""
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "pic.jpg").write_text("data")

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("No empty directories found.")


def test_clean_singular_message_for_one_directory(tmp_path: Path) -> None:
    """A single empty directory uses singular phrasing."""
    (tmp_path / "solo").mkdir()

    result = CliRunner().invoke(clean, [str(tmp_path), "--yes"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 1 empty directory.")


def test_clean_rejects_missing_directory(tmp_path: Path) -> None:
    """A nonexistent target directory fails validation."""
    result = CliRunner().invoke(main, ["clean", str(tmp_path / "missing")])

    assert_that(result.exit_code).is_not_equal_to(0)


def test_clean_reports_removal_failure_as_click_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filesystem failure during removal surfaces as a Click error."""
    (tmp_path / "solo").mkdir()

    def failing_remove(root: Path, **kwargs: object) -> list[Path]:
        raise FileSystemOperationError(
            "failed to remove empty directory",
            operation="remove_empty_tree",
            file_path=root / "solo",
        )

    monkeypatch.setattr(
        import_module("winnow.cli.clean"),
        "remove_empty_tree",
        failing_remove,
    )

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--yes"])

    assert_that(result.exit_code).is_equal_to(1)
    assert_that(result.output).contains("failed to remove empty directory")
    assert_that((tmp_path / "solo").exists()).is_true()
