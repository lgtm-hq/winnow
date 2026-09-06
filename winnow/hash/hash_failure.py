"""A single media file that could not be hashed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from winnow.exceptions import WinnowError


@dataclass(frozen=True, slots=True)
class HashFailure:
    """A file the batch hasher gave up on, with the error that stopped it.

    Args:
        path: Filesystem path of the file that failed.
        error: Domain error describing why hashing failed.
    """

    path: Path
    error: WinnowError
