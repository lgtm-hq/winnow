"""Shared enumeration types for winnow domain models."""

from __future__ import annotations

from enum import StrEnum, auto


class HashAlgorithm(StrEnum):
    """Hash algorithms supported for duplicate detection."""

    MD5 = auto()
    SHA256 = auto()
    AHASH = auto()
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


class SymlinkPolicy(StrEnum):
    """Symlink handling policies shared by configuration and path validation.

    :class:`~winnow.security.path_validator.PathValidator` treats the policy as
    binary: it either follows a symlink or refuses to traverse it. The
    difference between ``SKIP`` and ``ERROR`` is what the caller does after a
    refusal.

    Attributes:
        SKIP: Refuse to traverse symlinks. ``PathValidator`` raises
            :class:`~winnow.exceptions.SecurityError`; the caller skips the
            path silently.
        FOLLOW: Resolve symlinks and validate the real target against the
            allowed roots. ``PathValidator`` permits the symlink as long as
            its target stays inside a configured root.
        ERROR: Refuse to traverse symlinks. ``PathValidator`` raises
            :class:`~winnow.exceptions.SecurityError`; the caller records an
            error for the path.
    """

    SKIP = auto()
    FOLLOW = auto()
    ERROR = auto()
