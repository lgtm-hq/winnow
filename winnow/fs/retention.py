"""Retention helpers for the ``.winnow-backups`` directories written by backups.

:func:`winnow.fs.backup.create_backup` copies files with ``copy2``, which
preserves the original's modification time, so a backup's ``st_mtime`` says
nothing about when the backup was taken: a decade-old photo overwritten
yesterday would look stale immediately. The backup's ``st_ctime`` is set when
the staged copy is renamed into place, which is the backup time, so age is
always measured from ``st_ctime`` here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from winnow.fs.errors import FileSystemOperationError

__all__ = [
    "BACKUP_DIRNAME",
    "PrunePlan",
    "find_backup_directories",
    "plan_backup_prune",
    "prune_backups",
]

BACKUP_DIRNAME: Final[str] = ".winnow-backups"

StatFn = Callable[[Path], os.stat_result]


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """Backup files selected for removal and their combined size."""

    paths: tuple[Path, ...]
    bytes_total: int


def find_backup_directories(root: Path) -> list[Path]:
    """Find every backup directory under ``root``.

    A backup directory is never descended into, so a ``.winnow-backups`` nested
    inside another backup directory (a backed-up directory tree) is not listed.

    Args:
        root: Directory to search within.

    Returns:
        Backup directories in sorted order.
    """
    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        if BACKUP_DIRNAME in dirnames:
            found.append(Path(dirpath) / BACKUP_DIRNAME)
            dirnames.remove(BACKUP_DIRNAME)
    return sorted(found)


def plan_backup_prune(
    root: Path,
    *,
    max_age: timedelta,
    now: datetime | None = None,
    stat_fn: StatFn | None = None,
) -> PrunePlan:
    """Select backup files under ``root`` older than ``max_age``.

    Only regular files directly inside a backup directory are considered. Age
    is measured from ``st_ctime`` (see the module docstring for why ``st_mtime``
    is unsuitable).

    Args:
        root: Directory whose backup directories are inspected.
        max_age: Maximum age a backup may reach before it is selected. A
            non-positive value disables pruning and yields an empty plan.
        now: Reference time; defaults to the current UTC time.
        stat_fn: Function used to stat each candidate; defaults to
            ``Path.stat`` resolved at call time. Injectable for tests because
            ``st_ctime`` cannot be backdated portably.

    Returns:
        The selected paths in sorted order and their combined size.
    """
    if max_age <= timedelta(0):
        return PrunePlan(paths=(), bytes_total=0)
    reference = now if now is not None else datetime.now(UTC)
    stat = stat_fn if stat_fn is not None else Path.stat
    cutoff = reference.timestamp() - max_age.total_seconds()
    selected: list[Path] = []
    bytes_total = 0
    for backup_dir in find_backup_directories(root):
        for candidate in backup_dir.iterdir():
            if not candidate.is_file() or candidate.is_symlink():
                continue
            stat_result = stat(candidate)
            if stat_result.st_ctime >= cutoff:
                continue
            selected.append(candidate)
            bytes_total += stat_result.st_size
    return PrunePlan(paths=tuple(sorted(selected)), bytes_total=bytes_total)


def prune_backups(plan: PrunePlan) -> list[Path]:
    """Remove the files in ``plan`` and any backup directory left empty.

    Every path is attempted even when an earlier one fails; failures are
    aggregated into a single error raised after the sweep.

    Args:
        plan: Files to remove, as produced by :func:`plan_backup_prune`.

    Returns:
        Paths that were removed.

    Raises:
        FileSystemOperationError: If any file or emptied directory could not
            be removed; ``details["errors"]`` lists each failure.
    """
    removed: list[Path] = []
    errors: list[str] = []
    for path in plan.paths:
        try:
            path.unlink()
        except OSError as error:
            errors.append(f"{path}: {error}")
        else:
            removed.append(path)
    for backup_dir in sorted({path.parent for path in removed}):
        try:
            if not any(backup_dir.iterdir()):
                backup_dir.rmdir()
        except OSError as error:
            errors.append(f"{backup_dir}: {error}")
    if errors:
        raise FileSystemOperationError(
            "failed to prune backups",
            operation="prune_backups",
            details={"errors": errors},
        )
    return removed
