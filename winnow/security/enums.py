"""Enumerations for the winnow security domain."""

from __future__ import annotations

from enum import StrEnum, auto


class SymlinkPolicy(StrEnum):
    """Policy governing how symbolic links are handled during validation.

    Attributes:
        FOLLOW: Resolve symlinks and validate the real target against the
            allowed roots. A symlink is permitted as long as its resolved
            target stays inside a configured root.
        REJECT: Treat any symlink encountered along the path as a violation
            and raise a :class:`~winnow.exceptions.SecurityError`.
        WARN: Resolve symlinks like :attr:`FOLLOW`, but emit a warning for
            every symlink encountered so operators can audit their use.
    """

    FOLLOW = auto()
    REJECT = auto()
    WARN = auto()
