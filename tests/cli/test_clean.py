"""Tests for the ``winnow clean`` command and its helpers."""

from __future__ import annotations

from pathlib import Path

from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.clean import (
    clean,
    find_empty_directories,
    remove_empty_directories,
)


def _make_tree(root: Path) -> None:
    """Create a sample directory tree with mixed empty and populated dirs.

    Args:
        root: Directory to populate.
    """
    (root / "photos" / "2020").mkdir(parents=True)
    (root / "photos" / "2020" / "pic.jpg").write_text("data")
    (root / "empty" / "nested" / "leaf").mkdir(parents=True)
    (root / "empty2").mkdir()


def test_find_empty_directories_cascades_bottom_up(tmp_path: Path) -> None:
    """Nested empty directories are reported children-before-parents."""
    _make_tree(tmp_path)

    result = find_empty_directories(tmp_path)

    assert_that(result).contains_only(
        tmp_path / "empty" / "nested" / "leaf",
        tmp_path / "empty" / "nested",
        tmp_path / "empty",
        tmp_path / "empty2",
    )
    # os.walk guarantees children before parents but not sibling order, so
    # assert only the child-before-parent invariant.
    for index, path in enumerate(result):
        for descendant in result[index + 1 :]:
            assert_that(descendant.is_relative_to(path)).is_false()


def test_find_empty_directories_preserves_populated_and_root(tmp_path: Path) -> None:
    """Directories containing files and the root itself are never returned."""
    _make_tree(tmp_path)

    result = find_empty_directories(tmp_path)

    assert_that(result).does_not_contain(tmp_path)
    assert_that(result).does_not_contain(tmp_path / "photos")
    assert_that(result).does_not_contain(tmp_path / "photos" / "2020")


def test_find_empty_directories_honors_exclude_patterns(tmp_path: Path) -> None:
    """An excluded directory and its ancestors are preserved."""
    (tmp_path / "keep" / ".git").mkdir(parents=True)
    (tmp_path / "gone").mkdir()

    result = find_empty_directories(tmp_path, exclude_patterns=[".git"])

    assert_that(result).contains(tmp_path / "gone")
    assert_that(result).does_not_contain(tmp_path / "keep" / ".git")
    assert_that(result).does_not_contain(tmp_path / "keep")


def test_find_empty_directories_matches_relative_pattern(tmp_path: Path) -> None:
    """Exclude patterns match paths relative to the root."""
    (tmp_path / "cache" / "tmp").mkdir(parents=True)

    result = find_empty_directories(tmp_path, exclude_patterns=["cache/*"])

    assert_that(result).does_not_contain(tmp_path / "cache" / "tmp")
    assert_that(result).does_not_contain(tmp_path / "cache")


def test_remove_empty_directories_deletes_in_order(tmp_path: Path) -> None:
    """Removal deletes each reported directory."""
    _make_tree(tmp_path)
    candidates = find_empty_directories(tmp_path)

    removed = remove_empty_directories(candidates)

    assert_that(removed).is_equal_to(candidates)
    assert_that((tmp_path / "empty").exists()).is_false()
    assert_that((tmp_path / "empty2").exists()).is_false()
    assert_that((tmp_path / "photos" / "2020" / "pic.jpg").exists()).is_true()


def test_clean_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    """A dry run lists candidates and leaves the tree intact."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("4 empty directories to remove (dry run)")
    assert_that((tmp_path / "empty").exists()).is_true()


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
