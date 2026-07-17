"""Enumeration types for filesystem operation tracking."""

from __future__ import annotations

from enum import StrEnum, auto


class FileOperation(StrEnum):
    """Filesystem operations recorded by atomic helpers."""

    BACKUP = auto()
    COPY = auto()
    DELETE = auto()
    MKDIR = auto()
    MOVE = auto()
    RESTORE = auto()


class OperationStatus(StrEnum):
    """Lifecycle states for recorded filesystem operations."""

    APPLIED = auto()
    ROLLED_BACK = auto()
