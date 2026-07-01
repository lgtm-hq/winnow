"""Shared enumeration types for winnow domain models."""

from __future__ import annotations

from enum import StrEnum, auto


class HashAlgorithm(StrEnum):
    """Hash algorithms supported for duplicate detection."""

    MD5 = auto()
    SHA256 = auto()
    PHASH = auto()
    DHASH = auto()
    WHASH = auto()


class SortOrder(StrEnum):
    """Sort order for duplicate resolution and reporting."""

    BY_NAME = auto()
    BY_SIZE = auto()
    BY_DATE = auto()
    BY_QUALITY = auto()


class MediaCategory(StrEnum):
    """High-level media categories for filtering and configuration."""

    ALL = auto()
    PHOTOS = auto()
    VIDEOS = auto()
    AUDIO = auto()


class FileAction(StrEnum):
    """Actions that can be taken on duplicate or candidate files."""

    KEEP = auto()
    DELETE = auto()
    MOVE = auto()
    REVIEW = auto()
    SKIP = auto()
