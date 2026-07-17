"""Filesystem-specific exceptions."""

from __future__ import annotations

from winnow.exceptions import WinnowError


class FileSystemOperationError(WinnowError):
    """Raised when an atomic filesystem operation cannot be applied."""


class FileSystemRollbackError(WinnowError):
    """Raised when an explicit rollback cannot fully restore prior state."""
