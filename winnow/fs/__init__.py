"""Atomic filesystem operations and backup helpers."""

from __future__ import annotations

from winnow.fs.backup import create_backup, restore_backup
from winnow.fs.backup_options import BackupOptions
from winnow.fs.errors import FileSystemOperationError, FileSystemRollbackError
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation, OperationStatus
from winnow.fs.transaction import (
    FileSystemTransaction,
    atomic_copy,
    atomic_delete,
    atomic_mkdir,
    atomic_move,
    transactional_file_ops,
)

__all__ = [
    "BackupOptions",
    "FileOperation",
    "FileSystemOperationError",
    "FileSystemRollbackError",
    "FileSystemTransaction",
    "OperationLog",
    "OperationStatus",
    "atomic_copy",
    "atomic_delete",
    "atomic_mkdir",
    "atomic_move",
    "create_backup",
    "restore_backup",
    "transactional_file_ops",
]
