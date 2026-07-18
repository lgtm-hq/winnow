"""Shared support types and validation helpers for per-operation modules.

The per-operation modules (:mod:`winnow.fs._copy`, :mod:`winnow.fs._move`,
:mod:`winnow.fs._delete`, :mod:`winnow.fs._mkdir`) each apply one filesystem
operation and hand its lifecycle hooks back to
:class:`winnow.fs.transaction.FileSystemTransaction`. This module holds the
result type and the small validation helpers those modules share.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from winnow.fs._path_ops import path_exists
from winnow.fs.errors import FileSystemOperationError
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation

RollbackStep = Callable[[], None]
CommitStep = Callable[[], None]


@dataclass(frozen=True, slots=True)
class AppliedOperation:
    """A successfully applied filesystem operation and its lifecycle hooks.

    Attributes:
        log: Structured log entry for the applied operation.
        rollback: Callable that restores the pre-operation state.
        commit: Callable that discards staging artifacts after commit.
    """

    log: OperationLog
    rollback: RollbackStep
    commit: CommitStep


def operation_error(
    *,
    operation: FileOperation,
    path: Path,
    error: OSError | shutil.Error,
) -> FileSystemOperationError:
    """Build a structured filesystem operation error.

    Args:
        operation: Operation that failed.
        path: Path the operation was applied to.
        error: Underlying filesystem error.

    Returns:
        Structured error describing the failed operation.
    """
    return FileSystemOperationError(
        f"failed to apply filesystem {operation.value}",
        operation=f"fs.{operation.value}",
        file_path=path,
        details={"error": str(error)},
    )


def validate_destination_parent(destination: Path) -> None:
    """Ensure a destination parent directory exists.

    Args:
        destination: Destination path whose parent must exist.

    Raises:
        FileNotFoundError: When the destination parent is not a directory.
    """
    if not destination.parent.is_dir():
        raise FileNotFoundError(destination.parent)


def validate_source(
    *,
    source: Path,
    operation: FileOperation,
) -> None:
    """Ensure a source path exists before applying an operation.

    Args:
        source: Source path the operation reads from.
        operation: Operation being validated, used in the error message.

    Raises:
        FileNotFoundError: When the source path does not exist.
    """
    if not path_exists(source):
        raise FileNotFoundError(f"{operation.value} source not found: {source}")
