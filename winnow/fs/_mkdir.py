"""Mkdir operation: forward apply and rollback of created directories.

Patchable filesystem helpers (``_missing_directories``, ...) are resolved
through :mod:`winnow.fs.transaction` at call time so failure-injection tests
keep a single stable patch point.
"""

from __future__ import annotations

from pathlib import Path

# Circular by design: winnow.fs.transaction imports this module and hosts the
# patchable helper bindings. Attributes are only resolved at call time, after
# both modules have fully initialized.
from winnow.fs import transaction as _txn
from winnow.fs._operation import AppliedOperation, operation_error
from winnow.fs._path_ops import path_exists
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation


def apply_mkdir(
    *,
    path: Path,
    parents: bool,
    exist_ok: bool,
) -> AppliedOperation:
    """Create a directory and prepare rollback for created directories.

    Args:
        path: Directory path to create.
        parents: Whether missing parent directories should be created.
        exist_ok: Whether an existing directory should be accepted.

    Returns:
        Applied operation with its log entry and lifecycle hooks.

    Raises:
        FileSystemOperationError: When the directory cannot be created.
    """
    created_paths: list[Path] = []

    try:
        if path_exists(path):
            if exist_ok and path.is_dir():
                log = OperationLog(
                    operation=FileOperation.MKDIR,
                    destination=path,
                )
                return AppliedOperation(
                    log=log,
                    rollback=lambda: None,
                    commit=lambda: None,
                )
            raise FileExistsError(path)
        created_paths = _create_directories(path=path, parents=parents)
    except OSError as error:
        failure = operation_error(
            operation=FileOperation.MKDIR,
            path=path,
            error=error,
        )
        _txn._run_cleanups(
            failure,
            [lambda: rollback_mkdir(created_paths)],
        )
        raise failure from error

    log = OperationLog(
        operation=FileOperation.MKDIR,
        destination=path,
        created_paths=tuple(created_paths),
    )
    return AppliedOperation(
        log=log,
        rollback=lambda: rollback_mkdir(created_paths),
        commit=lambda: None,
    )


def rollback_mkdir(created_paths: list[Path]) -> None:
    """Roll back directories created by a mkdir operation.

    Args:
        created_paths: Directories created by the operation, top-down.
    """
    for created_path in reversed(created_paths):
        _cleanup_empty_directory(created_path)


def _cleanup_empty_directory(path: Path) -> None:
    """Remove an empty directory, ignoring paths already removed.

    Args:
        path: Directory path to remove.
    """
    try:
        path.rmdir()
    except FileNotFoundError:
        return


def _create_directories(
    *,
    path: Path,
    parents: bool,
) -> list[Path]:
    """Create a directory path and return directories created.

    Args:
        path: Directory path to create.
        parents: Whether missing parent directories should be created.

    Returns:
        Directories created by this call, ordered from highest parent to leaf.
    """
    if not parents:
        path.mkdir()
        return [path]

    created_paths: list[Path] = []
    for missing_path in _txn._missing_directories(path):
        missing_path.mkdir()
        created_paths.append(missing_path)
    return created_paths
