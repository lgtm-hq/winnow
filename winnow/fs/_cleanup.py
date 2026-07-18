"""Shared cleanup and tombstone-restore helpers.

These helpers used to live as diverging private copies in both
:mod:`winnow.fs.backup` and :mod:`winnow.fs.transaction`. They are kept in one
internal module so rollback and cleanup fixes land in a single place, with the
stricter error-aggregation semantics as the only behavior.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from winnow.fs._path_ops import path_exists, remove_path
from winnow.fs.errors import FileSystemOperationError

CleanupStep = Callable[[], object]
RemoveStep = Callable[[Path], None]


def cleanup_path(
    path: Path,
    *,
    remove: RemoveStep = remove_path,
) -> None:
    """Remove a path when it exists.

    Args:
        path: File, directory, or symlink path to remove.
        remove: Removal primitive to apply. Callers may pass their own
            module's binding so removal policy stays overridable per caller.
    """
    if path_exists(path):
        remove(path)


def missing_directories(path: Path) -> list[Path]:
    """Return missing directories from highest parent to leaf.

    Args:
        path: Directory path whose missing ancestors should be collected.

    Returns:
        Missing directories ordered so they can be created top-down.
    """
    missing_paths: list[Path] = []
    cursor = path
    while not path_exists(cursor):
        missing_paths.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    missing_paths.reverse()
    return missing_paths


def restore_tombstone(
    *,
    tombstone: Path,
    destination: Path,
) -> None:
    """Restore a tombstoned destination if the tombstone was created.

    Every step is attempted even when an earlier one fails: secondary
    failures are attached to the first failure via ``add_note`` and the first
    failure is re-raised, so a partial restore never fails silently.

    Args:
        tombstone: Staged tombstone path holding the prior destination.
        destination: Destination path to restore the tombstone to.

    Raises:
        OSError: When clearing the destination or moving the tombstone back
            fails. Secondary failures are attached as notes.
        shutil.Error: When removing the stale destination tree fails.
    """
    if not path_exists(tombstone):
        return
    errors: list[Exception] = []
    if path_exists(destination):
        try:
            remove_path(destination)
        except (OSError, shutil.Error) as cleanup_error:
            errors.append(cleanup_error)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tombstone.replace(destination)
    except OSError as cleanup_error:
        errors.append(cleanup_error)
    if errors:
        for secondary_error in errors[1:]:
            errors[0].add_note(f"tombstone restore failed: {secondary_error}")
        raise errors[0]


def run_cleanups(
    error: BaseException,
    cleanups: Sequence[CleanupStep],
) -> None:
    """Run cleanup callables without masking the original operation error.

    Each callable is executed defensively so a cleanup failure never replaces
    the error that triggered the handler. Any cleanup failure is attached to
    ``error`` as a note for diagnostics, including nested notes raised by
    aggregating cleanups.

    Args:
        error: Operation error to annotate with cleanup failures.
        cleanups: Cleanup callables to execute in order.
    """
    for cleanup in cleanups:
        try:
            cleanup()
        except (OSError, shutil.Error, FileSystemOperationError) as cleanup_error:
            error.add_note(f"cleanup failed: {cleanup_error}")
            for note in getattr(cleanup_error, "__notes__", ()):
                error.add_note(f"cleanup detail: {note}")
