"""Tests for the shared read-only walk in :mod:`winnow.fs.walk`."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that

from winnow.fs.walk import (
    SYMLINK_LOOP,
    WalkEvent,
    WalkEventKind,
    WalkPolicy,
    is_always_skipped,
    is_excluded,
    walk_files,
)
from winnow.models.config import OrganizeSettings, PathSettings, WinnowConfig
from winnow.models.enums import SymlinkPolicy


def _touch(path: Path) -> Path:
    """Create an empty file, with its parent directories.

    Args:
        path: File to create.

    Returns:
        The path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _walk(
    root: Path,
    policy: WalkPolicy | None = None,
) -> tuple[list[str], list[WalkEvent]]:
    """Walk ``root`` and return relative yielded paths and the events.

    Args:
        root: Directory to walk.
        policy: Inclusion rules; defaults to :class:`WalkPolicy`.

    Returns:
        Yielded paths relative to ``root`` in walk order, and every event.
    """
    events: list[WalkEvent] = []
    yielded = [
        Path(entry.path).relative_to(root).as_posix()
        for entry in walk_files(
            root,
            policy=policy or WalkPolicy(),
            on_event=events.append,
        )
    ]
    return yielded, events


def test_walk_files_yields_sorted_files_before_subdirectories(tmp_path: Path) -> None:
    """Files of a directory come before its subdirectories, sorted by name."""
    for name in ("b.txt", "a/z.txt", "a/deep/m.txt", "c/k.txt", "a.txt"):
        _touch(tmp_path / name)

    yielded, events = _walk(tmp_path)

    assert_that(yielded).is_equal_to(
        ["a.txt", "b.txt", "a/z.txt", "a/deep/m.txt", "c/k.txt"],
    )
    assert_that(events).is_empty()


def test_walk_files_reports_hidden_and_excluded_as_silent_skips(
    tmp_path: Path,
) -> None:
    """Hidden entries and pattern matches are skipped without an issue."""
    _touch(tmp_path / "keep.txt")
    _touch(tmp_path / ".winnow-staging" / "x.txt")
    _touch(tmp_path / ".DS_Store")
    _touch(tmp_path / "drafts" / "d.txt")

    yielded, events = _walk(tmp_path, WalkPolicy(exclude_patterns=("drafts",)))

    assert_that(yielded).is_equal_to(["keep.txt"])
    assert_that([(event.kind, event.path.name) for event in events]).is_equal_to(
        [
            (WalkEventKind.HIDDEN, ".DS_Store"),
            (WalkEventKind.HIDDEN, ".winnow-staging"),
            (WalkEventKind.EXCLUDED, "drafts"),
        ],
    )
    assert_that([event.is_issue for event in events]).does_not_contain(True)


def test_walk_files_matches_patterns_against_relative_paths(tmp_path: Path) -> None:
    """A pattern with a slash matches the path relative to the root."""
    _touch(tmp_path / "a" / "skip.txt")
    _touch(tmp_path / "b" / "skip.txt")

    yielded, _ = _walk(tmp_path, WalkPolicy(exclude_patterns=("a/skip.txt",)))

    assert_that(yielded).is_equal_to(["b/skip.txt"])


def test_walk_files_skip_policy_refuses_symlinks_silently(tmp_path: Path) -> None:
    """Under SKIP a symlink is a silent skip."""
    target = _touch(tmp_path / "a.txt")
    (tmp_path / "link.txt").symlink_to(target)

    yielded, events = _walk(tmp_path)

    assert_that(yielded).is_equal_to(["a.txt"])
    assert_that(events).is_length(1)
    assert_that(events[0].kind).is_equal_to(WalkEventKind.SYMLINK)
    assert_that(events[0].is_issue).is_false()


def test_walk_files_error_policy_reports_symlinks_as_issues(tmp_path: Path) -> None:
    """Under ERROR a refused symlink is an issue."""
    target = _touch(tmp_path / "a.txt")
    (tmp_path / "link.txt").symlink_to(target)

    _, events = _walk(tmp_path, WalkPolicy(symlink_policy=SymlinkPolicy.ERROR))

    assert_that(events).is_length(1)
    assert_that(events[0].kind).is_equal_to(WalkEventKind.SYMLINK)
    assert_that(events[0].is_issue).is_true()


def test_walk_files_follow_admits_links_and_visits_a_directory_once(
    tmp_path: Path,
) -> None:
    """Under FOLLOW linked files are yielded and a linked directory loop ends."""
    _touch(tmp_path / "d" / "f.txt")
    (tmp_path / "d" / "loop").symlink_to(tmp_path / "d")
    (tmp_path / "file_link.txt").symlink_to(tmp_path / "d" / "f.txt")

    yielded, events = _walk(tmp_path, WalkPolicy(symlink_policy=SymlinkPolicy.FOLLOW))

    assert_that(yielded).is_equal_to(["file_link.txt", "d/f.txt"])
    assert_that(events).is_empty()


def test_walk_files_reports_unreadable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory that cannot be listed is one issue; the walk goes on."""
    _touch(tmp_path / "a.txt")
    locked = tmp_path / "locked"
    _touch(locked / "inside.txt")
    _touch(tmp_path / "z" / "after.txt")
    real_scandir = os.scandir

    def fake_scandir(directory: Any) -> Any:
        if Path(directory) == locked:
            raise PermissionError(13, "Permission denied", str(directory))
        return real_scandir(directory)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    yielded, events = _walk(tmp_path)

    assert_that(yielded).is_equal_to(["a.txt", "z/after.txt"])
    assert_that(events).is_length(1)
    assert_that(events[0].kind).is_equal_to(WalkEventKind.UNREADABLE_DIRECTORY)
    assert_that(events[0].path).is_equal_to(locked)
    assert_that(events[0].message).is_equal_to("Permission denied")
    assert_that(events[0].is_issue).is_true()


def test_walk_files_honours_max_depth(tmp_path: Path) -> None:
    """``max_depth`` limits how far below the root the walk descends."""
    for name in ("top.txt", "a/one.txt", "a/b/two.txt"):
        _touch(tmp_path / name)

    shallow, _ = _walk(tmp_path, WalkPolicy(max_depth=0))
    one_level, _ = _walk(tmp_path, WalkPolicy(max_depth=1))

    assert_that(shallow).is_equal_to(["top.txt"])
    assert_that(one_level).is_equal_to(["top.txt", "a/one.txt"])


def test_walk_policy_from_config_copies_discovery_settings() -> None:
    """The policy carries the config's patterns, symlink policy and depth."""
    config = WinnowConfig(
        paths=PathSettings(exclude_patterns=["*.tmp"]),
        organize=OrganizeSettings(max_depth=3),
        follow_symlinks=True,
        symlink_policy=SymlinkPolicy.FOLLOW,
    )

    policy = WalkPolicy.from_config(config)
    flat = WalkPolicy.from_config(config, recursive=False)

    assert_that(policy).is_equal_to(
        WalkPolicy(
            exclude_patterns=("*.tmp",),
            symlink_policy=SymlinkPolicy.FOLLOW,
            max_depth=3,
        ),
    )
    assert_that(flat.max_depth).is_equal_to(0)


def test_name_rules() -> None:
    """Dot entries and winnow folders are always skipped; patterns match names."""
    assert_that(is_always_skipped(".hidden")).is_true()
    assert_that(is_always_skipped("photo.jpg")).is_false()
    assert_that(
        is_excluded(name="x.tmp", relative="a/x.tmp", patterns=("*.tmp",)),
    ).is_true()
    assert_that(
        is_excluded(name="x.jpg", relative="a/x.jpg", patterns=("*.tmp",)),
    ).is_false()


class _BrokenEntry:
    """A directory entry whose type checks fail, as on a vanished target.

    Attributes:
        name: Entry name.
        path: Entry path as a string.
    """

    def __init__(self, path: Path, *, fail_symlink: bool) -> None:
        self.name = path.name
        self.path = str(path)
        self._fail_symlink = fail_symlink

    def is_symlink(self) -> bool:
        """Report whether the entry is a symlink.

        Returns:
            ``False`` unless set to fail.

        Raises:
            OSError: When set to fail.
        """
        if self._fail_symlink:
            raise OSError(5, "Input/output error")
        return False

    def is_dir(self) -> bool:
        """Report whether the entry is a directory.

        Raises:
            OSError: Always.
        """
        raise OSError(5, "Input/output error")


def test_walk_files_reports_entries_that_cannot_be_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry whose type cannot be read is one issue and is not yielded."""
    entries = [
        _BrokenEntry(tmp_path / "a", fail_symlink=True),
        _BrokenEntry(tmp_path / "b", fail_symlink=False),
    ]

    def fake_scandir(directory: Any) -> Any:
        del directory
        return contextlib.nullcontext(entries)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    yielded, events = _walk(tmp_path)

    assert_that(yielded).is_empty()
    assert_that([(event.kind, event.path.name) for event in events]).is_equal_to(
        [
            (WalkEventKind.UNREADABLE_ENTRY, "a"),
            (WalkEventKind.UNREADABLE_ENTRY, "b"),
        ],
    )
    assert_that({event.message for event in events}).is_equal_to(
        {"Input/output error"},
    )


def _loop_on(monkeypatch: pytest.MonkeyPatch, looping: Path) -> None:
    """Make ``Path.resolve`` raise as Python 3.11 and 3.12 do on a loop.

    Those versions raise ``RuntimeError("Symlink loop from ...")`` from
    ``Path.resolve`` for a symlink cycle; later versions return a path. The
    patch reproduces the older behaviour on every interpreter.

    Args:
        monkeypatch: Fixture used to replace ``Path.resolve``.
        looping: The path whose resolution loops.
    """
    real_resolve = Path.resolve

    def resolve(self: Path, *, strict: bool = False) -> Path:
        if self == looping:
            raise RuntimeError(f"Symlink loop from {str(self)!r}")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)


@pytest.mark.parametrize(
    ("policy", "kind", "is_issue"),
    [
        (SymlinkPolicy.SKIP, WalkEventKind.SYMLINK, False),
        (SymlinkPolicy.ERROR, WalkEventKind.SYMLINK, True),
        (SymlinkPolicy.FOLLOW, WalkEventKind.UNREADABLE_ENTRY, True),
    ],
)
def test_walk_files_reports_a_symlink_whose_resolution_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: SymlinkPolicy,
    kind: WalkEventKind,
    is_issue: bool,
) -> None:
    """A ``RuntimeError`` from resolving a link is one event, not a crash."""
    _touch(tmp_path / "a.txt")
    link = tmp_path / "self_link"
    link.symlink_to(link)
    _touch(tmp_path / "z.txt")
    _loop_on(monkeypatch, link)

    yielded, events = _walk(tmp_path, WalkPolicy(symlink_policy=policy))

    assert_that(yielded).is_equal_to(["a.txt", "z.txt"])
    assert_that(events).is_length(1)
    assert_that(events[0].kind).is_equal_to(kind)
    assert_that(events[0].path).is_equal_to(link)
    assert_that(events[0].message).is_equal_to(SYMLINK_LOOP)
    assert_that(events[0].is_issue).is_equal_to(is_issue)


@pytest.mark.parametrize("policy", list(SymlinkPolicy))
def test_walk_files_survives_a_real_self_link(
    tmp_path: Path,
    *,
    policy: SymlinkPolicy,
) -> None:
    """A link to itself is one event under every policy and Python version."""
    _touch(tmp_path / "a.txt")
    (tmp_path / "self_link").symlink_to(tmp_path / "self_link")

    yielded, events = _walk(tmp_path, WalkPolicy(symlink_policy=policy))

    assert_that(yielded).is_equal_to(["a.txt"])
    assert_that(events).is_length(1)
    assert_that(events[0].path.name).is_equal_to("self_link")
    expected = (
        WalkEventKind.UNREADABLE_ENTRY
        if policy is SymlinkPolicy.FOLLOW
        else WalkEventKind.SYMLINK
    )
    assert_that(events[0].kind).is_equal_to(expected)


@pytest.mark.parametrize("policy", list(SymlinkPolicy))
def test_walk_files_survives_a_link_to_an_ancestor(
    tmp_path: Path,
    *,
    policy: SymlinkPolicy,
) -> None:
    """A link back to an ancestor never walks a directory twice."""
    _touch(tmp_path / "a.txt")
    _touch(tmp_path / "sub" / "b.txt")
    (tmp_path / "sub" / "up").symlink_to(tmp_path)

    yielded, events = _walk(tmp_path, WalkPolicy(symlink_policy=policy))

    assert_that(yielded).is_equal_to(["a.txt", "sub/b.txt"])
    if policy is SymlinkPolicy.FOLLOW:
        assert_that(events).is_empty()
    else:
        assert_that([(event.kind, event.path.name) for event in events]).is_equal_to(
            [(WalkEventKind.SYMLINK, "up")],
        )


def test_walk_files_reports_a_queued_directory_that_stops_resolving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory that loops after it was listed is unreadable; the walk goes on."""
    _touch(tmp_path / "a.txt")
    broken = tmp_path / "m"
    _touch(broken / "inside.txt")
    _touch(tmp_path / "z" / "after.txt")
    _loop_on(monkeypatch, broken)

    yielded, events = _walk(tmp_path)

    assert_that(yielded).is_equal_to(["a.txt", "z/after.txt"])
    assert_that(events).is_length(1)
    assert_that(events[0].kind).is_equal_to(WalkEventKind.UNREADABLE_DIRECTORY)
    assert_that(events[0].path).is_equal_to(broken)
    assert_that(events[0].message).is_equal_to(SYMLINK_LOOP)
