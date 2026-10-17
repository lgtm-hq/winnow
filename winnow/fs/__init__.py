"""Atomic filesystem operations and backup helpers."""

from __future__ import annotations

from winnow.fs.backup import create_backup, restore_backup
from winnow.fs.backup_options import BackupOptions
from winnow.fs.empty_dirs import find_empty_directories, remove_empty_tree
from winnow.fs.errors import FileSystemOperationError, FileSystemRollbackError
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation, OperationStatus
from winnow.fs.retention import (
    BACKUP_DIRNAME,
    PrunePlan,
    find_backup_directories,
    plan_backup_prune,
    prune_backups,
)
from winnow.fs.transaction import (
    FileSystemTransaction,
    atomic_copy,
    atomic_delete,
    atomic_mkdir,
    atomic_move,
    transactional_file_ops,
)

__all__ = [
    "BACKUP_DIRNAME",
    "BackupOptions",
    "FileOperation",
    "FileSystemOperationError",
    "FileSystemRollbackError",
    "FileSystemTransaction",
    "OperationLog",
    "OperationStatus",
    "PrunePlan",
    "atomic_copy",
    "atomic_delete",
    "atomic_mkdir",
    "atomic_move",
    "create_backup",
    "find_backup_directories",
    "find_empty_directories",
    "plan_backup_prune",
    "prune_backups",
    "remove_empty_tree",
    "restore_backup",
    "transactional_file_ops",
]
