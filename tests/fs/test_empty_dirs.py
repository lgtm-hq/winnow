"""Tests for empty-directory discovery and removal in :mod:`winnow.fs`."""

from __future__ import annotations

import errno
import os
import stat
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from assertpy import assert_that

from winnow.fs import empty_dirs
from winnow.fs.empty_dirs import (
    EmptyDirScan,
    RemovalResult,
    find_empty_directories,
    remove_directories,
    remove_empty_tree,
    scan_empty_directories,
)
from winnow.fs.errors import FileSystemOperationError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Sequence

_WINDOWS = sys.platform.startswith("win")


@pytest.fixture(params=[False, True], ids=["ascending", "descending"])
def enumerated_walk_order(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Simulate both enumeration orders and honor top-down walk mutations."""
    real_walk = os.walk

    def reversed_walk(
        root: Path,
        *,
        topdown: bool = True,
        onerror: Callable[[OSError], None] | None = None,
        followlinks: bool = False,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        """Visit sibling subtrees in fixture order unless the caller reorders."""
        # os.walk returns a generator; typeshed exposes only Iterator.
        walk = cast(
            "Generator[tuple[str, list[str], list[str]], None, None]",
            real_walk(root, onerror=onerror, followlinks=followlinks),
        )
        with closing(walk):
            row = next(walk)
        row[1].sort(reverse=request.param)
        if topdown:
            yield row
        for name in row[1]:
            child = root / name
            if not followlinks and child.is_symlink():
                continue
            yield from reversed_walk(
                child,
                topdown=topdown,
                onerror=onerror,
                followlinks=followlinks,
            )
        if not topdown:
            yield row

    monkeypatch.setattr(os, "walk", reversed_walk)


def test_find_empty_directories_orders_sibling_subtrees(
    tmp_path: Path,
    enumerated_walk_order: None,
) -> None:
    """Both enumeration orders yield ascending postorder across depths."""
    (tmp_path / "first" / "z" / "leaf").mkdir(parents=True)
    (tmp_path / "first" / "a").mkdir()
    (tmp_path / "second").mkdir()

    result = find_empty_directories(tmp_path, include_root=True)

    assert_that(result).is_equal_to(
        [
            tmp_path / "first" / "a",
            tmp_path / "first" / "z" / "leaf",
            tmp_path / "first" / "z",
            tmp_path / "first",
            tmp_path / "second",
            tmp_path,
        ],
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


def _assert_children_before_parents(paths: Sequence[Path]) -> None:
    """Assert no path is followed by one of its own descendants.

    This helper checks the child-before-parent invariant independently of
    the sibling ordering covered by the reversed-enumeration regression.

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

    assert_that(result).is_equal_to(
        [
            tmp_path / "empty" / "nested" / "leaf",
            tmp_path / "empty" / "nested",
            tmp_path / "empty",
            tmp_path / "empty2",
        ],
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
    enumerated_walk_order: None,
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


def test_remove_directories_removes_exactly_the_given_paths(tmp_path: Path) -> None:
    """Only listed directories are removed, in the given order."""
    (tmp_path / "parent" / "child").mkdir(parents=True)
    (tmp_path / "unlisted").mkdir()
    planned = [tmp_path / "parent" / "child", tmp_path / "parent"]

    result = remove_directories(planned)

    assert_that(result).is_equal_to(
        RemovalResult(removed=tuple(planned), skipped=()),
    )
    assert_that((tmp_path / "parent").exists()).is_false()
    assert_that((tmp_path / "unlisted").exists()).is_true()


def test_remove_directories_skips_missing_path(tmp_path: Path) -> None:
    """A path that vanished is skipped and reported instead of raising."""
    (tmp_path / "present").mkdir()
    missing = tmp_path / "gone"

    result = remove_directories([missing, tmp_path / "present"])

    assert_that(result.removed).is_equal_to((tmp_path / "present",))
    assert_that(result.skipped).is_equal_to(((missing, "no longer exists"),))


def test_remove_directories_skips_non_empty_path_and_its_parent(
    tmp_path: Path,
) -> None:
    """A directory that gained a file is skipped, so its parent is too."""
    child = tmp_path / "parent" / "child"
    child.mkdir(parents=True)
    (child / "late.txt").write_text("data")

    result = remove_directories([child, tmp_path / "parent"])

    assert_that(result.removed).is_empty()
    assert_that(result.skipped).is_equal_to(
        (
            (child, "no longer empty"),
            (tmp_path / "parent", "no longer empty"),
        ),
    )
    assert_that((child / "late.txt").exists()).is_true()


@pytest.mark.skipif(_WINDOWS, reason="chmod 000 has no effect on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions",
)
def test_find_empty_directories_reports_unreadable_subtree(tmp_path: Path) -> None:
    """An unreadable subtree is reported and keeps itself and its ancestors."""
    locked = tmp_path / "parent" / "locked"
    locked.mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    locked.chmod(0)
    try:
        scan = scan_empty_directories(tmp_path)
        found = find_empty_directories(tmp_path)
    finally:
        locked.chmod(stat.S_IRWXU)

    assert_that(scan).is_equal_to(
        EmptyDirScan(candidates=(tmp_path / "plain",), unreadable=(locked,)),
    )
    assert_that(found).is_equal_to([tmp_path / "plain"])


def test_scan_empty_directories_reports_unreadable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listing error without a filename is attributed to the root."""

    def failing_walk(
        root: Path,
        *,
        topdown: bool = True,
        onerror: Callable[[OSError], None] | None = None,
        followlinks: bool = False,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        """Report one filename-less error and yield nothing."""
        if onerror is not None:
            onerror(OSError(errno.EIO, "I/O error"))
        yield from ()

    monkeypatch.setattr(os, "walk", failing_walk)

    scan = scan_empty_directories(tmp_path)

    assert_that(scan).is_equal_to(
        EmptyDirScan(candidates=(), unreadable=(tmp_path,)),
    )


def test_remove_directories_continues_after_other_rmdir_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another rmdir failure is recorded with its reason and removal goes on."""
    denied = tmp_path / "denied"
    denied.mkdir()
    (tmp_path / "later").mkdir()
    real_rmdir = Path.rmdir

    def denied_rmdir(self: Path) -> None:
        if self == denied:
            raise PermissionError(errno.EACCES, "denied", str(self))
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", denied_rmdir)

    result = remove_directories([denied, tmp_path / "later"])

    assert_that(result).is_equal_to(
        RemovalResult(
            removed=(tmp_path / "later",),
            skipped=((denied, "permission denied"),),
        ),
    )
    assert_that(denied.is_dir()).is_true()


def test_remove_directories_reports_path_replaced_by_file(tmp_path: Path) -> None:
    """A candidate replaced by a file is skipped as not a directory."""
    replaced = tmp_path / "replaced"
    replaced.write_text("data")
    (tmp_path / "later").mkdir()

    result = remove_directories([replaced, tmp_path / "later"])

    assert_that(result.removed).is_equal_to((tmp_path / "later",))
    assert_that(result.skipped).is_equal_to(((replaced, "not a directory"),))
    assert_that(replaced.is_file()).is_true()


def test_remove_directories_reports_unexpected_os_error_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmapped rmdir error is reported with the OS error text."""
    target = tmp_path / "busy"
    target.mkdir()

    def busy_rmdir(self: Path) -> None:
        raise OSError(errno.EBUSY, "Resource busy", str(self))

    monkeypatch.setattr(Path, "rmdir", busy_rmdir)

    result = remove_directories([target])

    assert_that(result.skipped).is_equal_to(((target, "Resource busy"),))


def _swap_parent_for_symlink(root: Path, outside: Path) -> None:
    """Replace ``root/parent`` with a symlink to ``outside/real``.

    Args:
        root: Scan root holding ``parent/child``.
        outside: Directory holding ``real/child`` outside the root.
    """
    (root / "parent" / "child").rmdir()
    (root / "parent").rmdir()
    (root / "parent").symlink_to(outside / "real", target_is_directory=True)


@pytest.fixture
def swapped_tree(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    """Plan ``root/parent/child`` and swap ``parent`` for an outside symlink.

    Returns:
        The scan root, the outside directory, and the planned candidates.
    """
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "parent" / "child").mkdir(parents=True)
    (outside / "real" / "child").mkdir(parents=True)
    planned = list(scan_empty_directories(root).candidates)
    _swap_parent_for_symlink(root, outside)
    return root, outside, planned


@pytest.mark.skipif(_WINDOWS, reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("dir_fd", [True, False], ids=["dir_fd", "lstat"])
def test_remove_directories_refuses_symlinked_ancestor(
    swapped_tree: tuple[Path, Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    dir_fd: bool,
) -> None:
    """A component swapped for a symlink never leads outside the root."""
    root, outside, planned = swapped_tree
    if not dir_fd:
        monkeypatch.setattr(os, "supports_dir_fd", set())

    result = remove_directories(planned, root=root)

    assert_that(result.removed).is_empty()
    assert_that(result.skipped).is_equal_to(
        (
            (root / "parent" / "child", "path changed"),
            (root / "parent", "path changed"),
        ),
    )
    assert_that((outside / "real" / "child").is_dir()).is_true()
    assert_that((root / "parent").is_symlink()).is_true()


@pytest.mark.parametrize("dir_fd", [True, False], ids=["dir_fd", "lstat"])
def test_remove_directories_with_root_removes_planned_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dir_fd: bool,
) -> None:
    """With a root, an unchanged plan is removed exactly as without one."""
    if not dir_fd:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    _make_tree(tmp_path)
    planned = find_empty_directories(tmp_path)
    (tmp_path / "gone").mkdir()
    (tmp_path / "gone").rmdir()
    planned.insert(0, tmp_path / "gone" / "child")

    result = remove_directories(planned, root=tmp_path)

    assert_that(result.removed).is_equal_to(tuple(planned[1:]))
    assert_that(result.skipped).is_equal_to(
        ((tmp_path / "gone" / "child", "no longer exists"),),
    )
    assert_that((tmp_path / "photos" / "2020" / "pic.jpg").is_file()).is_true()


def test_remove_directories_with_root_reports_file_component(
    tmp_path: Path,
) -> None:
    """An ancestor replaced by a file counts as a changed path."""
    (tmp_path / "parent").write_text("data")

    result = remove_directories([tmp_path / "parent" / "child"], root=tmp_path)

    assert_that(result.skipped).is_equal_to(
        ((tmp_path / "parent" / "child", "path changed"),),
    )


def test_remove_directories_with_root_reports_leaf_replaced_by_file(
    tmp_path: Path,
) -> None:
    """A leaf replaced by a file is skipped as not a directory."""
    (tmp_path / "leaf").write_text("data")

    result = remove_directories([tmp_path / "leaf"], root=tmp_path)

    assert_that(result.skipped).is_equal_to(
        ((tmp_path / "leaf", "not a directory"),),
    )


def test_remove_directories_with_missing_root_skips_everything(
    tmp_path: Path,
) -> None:
    """A root that vanished leaves every candidate skipped as missing."""
    root = tmp_path / "root"

    result = remove_directories([root / "a"], root=root)

    assert_that(result.skipped).is_equal_to(((root / "a", "no longer exists"),))


@pytest.mark.parametrize("dir_fd", [True, False], ids=["dir_fd", "lstat"])
def test_remove_directories_rejects_candidate_outside_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dir_fd: bool,
) -> None:
    """A candidate outside the root, or the root itself, is a caller bug."""
    if not dir_fd:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()

    with pytest.raises(ValueError, match="not below"):
        remove_directories([tmp_path / "elsewhere"], root=root)
    with pytest.raises(ValueError, match="not below"):
        remove_directories([root], root=root)
    assert_that((tmp_path / "elsewhere").is_dir()).is_true()


def test_remove_directories_with_root_reports_permission_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descriptor-relative rmdir failure is reported and removal goes on."""
    (tmp_path / "denied").mkdir()
    (tmp_path / "later").mkdir()
    real_rmdir = os.rmdir

    def denied_rmdir(path: str, *, dir_fd: int | None = None) -> None:
        if path == "denied":
            raise PermissionError(errno.EACCES, "denied", path)
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", denied_rmdir)
    monkeypatch.setattr(os, "supports_dir_fd", {denied_rmdir, os.open})

    result = remove_directories(
        [tmp_path / "denied", tmp_path / "later"],
        root=tmp_path,
    )

    assert_that(result.removed).is_equal_to((tmp_path / "later",))
    assert_that(result.skipped).is_equal_to(
        ((tmp_path / "denied", "permission denied"),),
    )


@pytest.mark.skipif(_WINDOWS, reason="chmod 000 has no effect on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions",
)
def test_scan_empty_directories_prunes_unreadable_excluded_directory(
    tmp_path: Path,
) -> None:
    """An excluded directory is not walked, so it is not reported unreadable."""
    excluded = tmp_path / "keep" / ".git"
    excluded.mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    excluded.chmod(0)
    try:
        scan = scan_empty_directories(tmp_path, exclude_patterns=[".git"])
    finally:
        excluded.chmod(stat.S_IRWXU)

    assert_that(scan).is_equal_to(
        EmptyDirScan(candidates=(tmp_path / "plain",), unreadable=()),
    )


@pytest.mark.skipif(_WINDOWS, reason="chmod 000 has no effect on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions",
)
def test_scan_empty_directories_sorts_unreadable_ascending(tmp_path: Path) -> None:
    """Unreadable directories are reported in ascending order."""
    locked = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    for directory in locked:
        directory.mkdir()
        directory.chmod(0)
    try:
        scan = scan_empty_directories(tmp_path)
    finally:
        for directory in locked:
            directory.chmod(stat.S_IRWXU)

    assert_that(scan.unreadable).is_equal_to(tuple(locked))


@pytest.mark.skipif(_WINDOWS, reason="chmod 000 has no effect on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions",
)
def test_remove_directories_with_root_reports_unopenable_ancestor(
    tmp_path: Path,
) -> None:
    """An ancestor that cannot be opened is reported and removal goes on."""
    locked = tmp_path / "locked"
    (locked / "child").mkdir(parents=True)
    (tmp_path / "later").mkdir()
    locked.chmod(0)
    try:
        result = remove_directories(
            [locked / "child", tmp_path / "later"],
            root=tmp_path,
        )
    finally:
        locked.chmod(stat.S_IRWXU)

    assert_that(result.removed).is_equal_to((tmp_path / "later",))
    assert_that(result.skipped).is_equal_to(
        ((locked / "child", "permission denied"),),
    )
    assert_that((locked / "child").is_dir()).is_true()
