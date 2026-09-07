"""Tests for backup retention planning and pruning."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.fs.errors import FileSystemOperationError
from winnow.fs.retention import (
    BACKUP_DIRNAME,
    PrunePlan,
    find_backup_directories,
    plan_backup_prune,
    prune_backups,
)

_NOW = datetime(2026, 1, 31, tzinfo=UTC)
_DAY_SECONDS = 86400


def _fake_stat(ages_days: dict[Path, int]) -> Callable[[Path], os.stat_result]:
    """Build a ``stat_fn`` whose ``st_ctime`` is backdated per path.

    Args:
        ages_days: Age in days to report for each path; others report ``_NOW``.

    Returns:
        A callable compatible with ``Path.stat`` returning a patched result.
    """

    def stat_fn(path: Path) -> os.stat_result:
        """Return the real stat with ``st_ctime`` replaced by the fake age."""
        real = path.stat()
        age = ages_days.get(path, 0)
        fields = list(real)
        fields[9] = _NOW.timestamp() - age * _DAY_SECONDS
        return os.stat_result(fields)

    return stat_fn


def _make_tree(root: Path) -> tuple[Path, Path, Path]:
    """Create two backup directories (one nested) plus non-backup noise.

    Args:
        root: Directory to populate.

    Returns:
        ``(old_backup, young_backup, unrelated_file)`` paths.
    """
    top_dir = root / BACKUP_DIRNAME
    top_dir.mkdir()
    old_backup = top_dir / "a.jpg.deadbeef.bak"
    old_backup.write_bytes(b"x" * 40)
    nested_dir = root / "photos" / "2020" / BACKUP_DIRNAME
    nested_dir.mkdir(parents=True)
    young_backup = nested_dir / "b.jpg.cafebabe.bak"
    young_backup.write_bytes(b"y" * 10)
    unrelated = root / "photos" / "old.jpg"
    unrelated.write_bytes(b"z" * 5)
    # A backup of a directory tree that itself contained a backup directory.
    inner = top_dir / "dir.deadbeef.bak" / BACKUP_DIRNAME
    inner.mkdir(parents=True)
    (inner / "inner.jpg.0.bak").write_bytes(b"w")
    return old_backup, young_backup, unrelated


def test_find_backup_directories_finds_nested_but_does_not_descend(
    tmp_path: Path,
) -> None:
    """Both backup dirs are found; a backup dir inside a backup dir is not."""
    _make_tree(tmp_path)

    found = find_backup_directories(tmp_path)

    assert_that(found).is_equal_to(
        [tmp_path / BACKUP_DIRNAME, tmp_path / "photos" / "2020" / BACKUP_DIRNAME],
    )


def test_plan_selects_only_files_older_than_max_age(tmp_path: Path) -> None:
    """Only the 40-day-old backup is listed, with its size as the total."""
    old_backup, young_backup, unrelated = _make_tree(tmp_path)
    stat_fn = _fake_stat({old_backup: 40, young_backup: 10, unrelated: 40})

    plan = plan_backup_prune(
        tmp_path,
        max_age=timedelta(days=30),
        now=_NOW,
        stat_fn=stat_fn,
    )

    assert_that(plan).is_equal_to(PrunePlan(paths=(old_backup,), bytes_total=40))


def test_plan_ignores_mtime(tmp_path: Path) -> None:
    """A fresh backup with an ancient mtime is not stale."""
    backup_dir = tmp_path / BACKUP_DIRNAME
    backup_dir.mkdir()
    backup = backup_dir / "a.jpg.0.bak"
    backup.write_bytes(b"x")
    ancient = (_NOW - timedelta(days=3650)).timestamp()
    os.utime(backup, times=(ancient, ancient))

    plan = plan_backup_prune(tmp_path, max_age=timedelta(days=30))

    assert_that(plan.paths).is_empty()


def test_plan_zero_max_age_is_empty(tmp_path: Path) -> None:
    """A zero ``max_age`` disables pruning even for stale files."""
    old_backup, _, _ = _make_tree(tmp_path)

    plan = plan_backup_prune(
        tmp_path,
        max_age=timedelta(0),
        now=_NOW,
        stat_fn=_fake_stat({old_backup: 400}),
    )

    assert_that(plan).is_equal_to(PrunePlan(paths=(), bytes_total=0))


def test_plan_skips_symlinks_and_subdirectories(tmp_path: Path) -> None:
    """Only regular files directly inside a backup dir are candidates."""
    backup_dir = tmp_path / BACKUP_DIRNAME
    backup_dir.mkdir()
    (backup_dir / "sub").mkdir()
    target = tmp_path / "target.jpg"
    target.write_bytes(b"t")
    (backup_dir / "link.jpg.0.bak").symlink_to(target)

    plan = plan_backup_prune(
        tmp_path,
        max_age=timedelta(days=1),
        now=_NOW + timedelta(days=400),
    )

    assert_that(plan.paths).is_empty()


def test_prune_removes_files_and_emptied_backup_dirs_only(tmp_path: Path) -> None:
    """The stale file and its now-empty dir go; the other backup dir stays."""
    old_backup, young_backup, unrelated = _make_tree(tmp_path)
    nested_dir = young_backup.parent
    nested_dir.joinpath("other.bak").unlink(missing_ok=True)
    plan = PrunePlan(paths=(young_backup,), bytes_total=10)

    removed = prune_backups(plan)

    assert_that(removed).is_equal_to([young_backup])
    assert_that(young_backup.exists()).is_false()
    assert_that(nested_dir.exists()).is_false()
    assert_that(nested_dir.parent.exists()).is_true()
    assert_that(old_backup.exists()).is_true()
    assert_that(unrelated.exists()).is_true()


def test_prune_keeps_backup_dir_that_is_not_empty(tmp_path: Path) -> None:
    """A backup directory still holding files is left in place."""
    backup_dir = tmp_path / BACKUP_DIRNAME
    backup_dir.mkdir()
    stale = backup_dir / "stale.bak"
    stale.write_bytes(b"s")
    fresh = backup_dir / "fresh.bak"
    fresh.write_bytes(b"f")

    prune_backups(PrunePlan(paths=(stale,), bytes_total=1))

    assert_that(stale.exists()).is_false()
    assert_that(fresh.exists()).is_true()
    assert_that(backup_dir.exists()).is_true()


def test_prune_aggregates_errors_and_removes_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing unlink is reported after the other files are removed."""
    backup_dir = tmp_path / BACKUP_DIRNAME
    backup_dir.mkdir()
    stuck = backup_dir / "stuck.bak"
    stuck.write_bytes(b"s")
    other = backup_dir / "other.bak"
    other.write_bytes(b"o")
    real_unlink = Path.unlink

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        """Fail for ``stuck`` and delegate for everything else."""
        if self == stuck:
            raise PermissionError("locked")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    with pytest.raises(FileSystemOperationError) as exc_info:
        prune_backups(PrunePlan(paths=(other, stuck), bytes_total=2))

    assert_that(other.exists()).is_false()
    assert_that(stuck.exists()).is_true()
    assert_that(backup_dir.exists()).is_true()
    errors = exc_info.value.context.details["errors"]
    assert_that(errors).is_instance_of(list).is_length(1)
    assert_that(str(errors)).contains("stuck.bak").contains("locked")
