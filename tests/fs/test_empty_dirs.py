"""Tests for empty-directory discovery and removal in :mod:`winnow.fs`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from assertpy import assert_that

from winnow.fs import empty_dirs
from winnow.fs.empty_dirs import find_empty_directories, remove_empty_tree
from winnow.fs.errors import FileSystemOperationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _make_tree(root: Path) -> None:
    """Create a sample directory tree with mixed empty and populated dirs.

    Args:
        root: Directory to populate.
    """
    (root / "photos" / "2020").mkdir(parents=True)
    (root / "photos" / "2020" / "pic.jpg").write_text("data")
    (root / "empty" / "nested" / "leaf").mkdir(parents=True)
    (root / "empty2").mkdir()


def _assert_children_before_parents(paths: Sequence[Path]) -> None:
    """Assert no path is followed by one of its own descendants.

    ``os.walk`` guarantees children before parents but not sibling order, so
    only the child-before-parent invariant is checked.

    Args:
        paths: Directories in the order they were reported.
    """
    for index, path in enumerate(paths):
        for descendant in paths[index + 1 :]:
            assert_that(descendant.is_relative_to(path)).is_false()


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
    _assert_children_before_parents(result)


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


def test_find_empty_directories_preserves_excluded_subtree(tmp_path: Path) -> None:
    """Directories nested inside an excluded subtree are preserved."""
    (tmp_path / ".git" / "refs" / "tags").mkdir(parents=True)

    result = find_empty_directories(tmp_path, exclude_patterns=[".git"])

    assert_that(result).is_empty()


def test_find_empty_directories_include_root_appends_root_last(
    tmp_path: Path,
) -> None:
    """With ``include_root`` an entirely empty tree ends with the root itself."""
    (tmp_path / "a" / "b").mkdir(parents=True)

    result = find_empty_directories(tmp_path, include_root=True)

    assert_that(result).is_equal_to([tmp_path / "a" / "b", tmp_path / "a", tmp_path])


def test_find_empty_directories_include_root_skips_root_with_files(
    tmp_path: Path,
) -> None:
    """With ``include_root`` a root holding a file is still never returned."""
    (tmp_path / "empty").mkdir()
    (tmp_path / "keep.txt").write_text("x")

    result = find_empty_directories(tmp_path, include_root=True)

    assert_that(result).is_equal_to([tmp_path / "empty"])


def test_find_empty_directories_include_root_skips_root_with_excluded_child(
    tmp_path: Path,
) -> None:
    """With ``include_root`` an excluded child keeps the root out too."""
    (tmp_path / ".git").mkdir()

    result = find_empty_directories(
        tmp_path,
        exclude_patterns=[".git"],
        include_root=True,
    )

    assert_that(result).is_empty()


def test_remove_empty_tree_deletes_children_before_parents(tmp_path: Path) -> None:
    """Removal deletes every reported directory and returns them in order."""
    _make_tree(tmp_path)
    expected = find_empty_directories(tmp_path)

    removed = remove_empty_tree(tmp_path, include_root=False)

    assert_that(removed).is_equal_to(expected)
    _assert_children_before_parents(removed)
    assert_that((tmp_path / "empty").exists()).is_false()
    assert_that((tmp_path / "empty2").exists()).is_false()
    assert_that((tmp_path / "photos" / "2020" / "pic.jpg").exists()).is_true()


def test_remove_empty_tree_honors_exclude_patterns(tmp_path: Path) -> None:
    """Excluded subtrees and their ancestors survive removal."""
    (tmp_path / "keep" / ".git").mkdir(parents=True)
    (tmp_path / "gone").mkdir()

    removed = remove_empty_tree(tmp_path, exclude_patterns=[".git"])

    assert_that(removed).is_equal_to([tmp_path / "gone"])
    assert_that((tmp_path / "keep" / ".git").is_dir()).is_true()
    assert_that(tmp_path.is_dir()).is_true()


def test_remove_empty_tree_removes_empty_root_by_default(tmp_path: Path) -> None:
    """An entirely empty tree is removed root and all."""
    root = tmp_path / "staging"
    (root / "a" / "b").mkdir(parents=True)

    removed = remove_empty_tree(root)

    assert_that(removed).is_equal_to([root / "a" / "b", root / "a", root])
    assert_that(root.exists()).is_false()


def test_remove_empty_tree_keeps_root_when_not_included(tmp_path: Path) -> None:
    """With ``include_root=False`` the empty root survives."""
    root = tmp_path / "staging"
    (root / "a").mkdir(parents=True)

    removed = remove_empty_tree(root, include_root=False)

    assert_that(removed).is_equal_to([root / "a"])
    assert_that(root.is_dir()).is_true()
    assert_that(list(root.iterdir())).is_empty()


def test_remove_empty_tree_keeps_root_containing_file(tmp_path: Path) -> None:
    """A root holding a file loses only its empty children."""
    (tmp_path / "empty").mkdir()
    (tmp_path / "keep.txt").write_text("x")

    removed = remove_empty_tree(tmp_path)

    assert_that(removed).is_equal_to([tmp_path / "empty"])
    assert_that((tmp_path / "keep.txt").is_file()).is_true()


def test_remove_empty_tree_on_populated_tree_returns_empty(tmp_path: Path) -> None:
    """A tree with no empty directories removes nothing."""
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "pic.jpg").write_text("data")

    assert_that(remove_empty_tree(tmp_path)).is_empty()
    assert_that((tmp_path / "photos" / "pic.jpg").is_file()).is_true()


def test_remove_empty_tree_wraps_rmdir_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory that gains a file after discovery raises a wrapped error."""
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    real_find = empty_dirs.find_empty_directories

    def racing_find(
        root: Path,
        *,
        exclude_patterns: Sequence[str] = (),
        include_root: bool = False,
    ) -> list[Path]:
        found = real_find(
            root,
            exclude_patterns=exclude_patterns,
            include_root=include_root,
        )
        (tmp_path / "second" / "late.txt").write_text("x")
        return found

    monkeypatch.setattr(empty_dirs, "find_empty_directories", racing_find)

    with pytest.raises(FileSystemOperationError) as exc_info:
        remove_empty_tree(tmp_path, include_root=False)

    error = exc_info.value
    assert_that(error.__cause__).is_instance_of(OSError)
    assert_that(error.context.operation).is_equal_to("remove_empty_tree")
    assert_that(error.context.file_path).is_equal_to(tmp_path / "second")
    assert_that((tmp_path / "first").exists()).is_false()
    assert_that((tmp_path / "second" / "late.txt").is_file()).is_true()
