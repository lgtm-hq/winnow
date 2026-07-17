"""Filename sanitization helpers for the winnow security domain.

These helpers produce filesystem-safe filenames that contain no path
separators, NUL bytes, or control characters. They are intentionally
conservative so that downstream features (for example, safe rename and
export flows) can rely on the returned value being a single, well-formed
path component.
"""

from __future__ import annotations

import os
import unicodedata

from winnow.exceptions import SecurityError

DEFAULT_MAX_LENGTH = 255
"""Conservative maximum filename length, in filesystem bytes, supported by
common filesystems (for example, the 255-byte component limit of ext4)."""

_PATH_SEPARATORS = frozenset({"/", "\\"})
"""Directory separators that must never appear inside a single component."""


def _is_forbidden_char(char: str) -> bool:
    """Return whether a character is unsafe inside a filename component.

    Args:
        char: Single character to evaluate.

    Returns:
        True if the character is a path separator, NUL byte, or other
        control character and must be replaced.
    """
    if char in _PATH_SEPARATORS:
        return True
    if char == "\x00":
        return True
    return unicodedata.category(char) in {"Cc", "Cf"}


def sanitize_filename(
    name: str,
    *,
    replacement: str = "_",
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """Return a filesystem-safe single path component derived from ``name``.

    Path separators, NUL bytes, and control characters are replaced with
    ``replacement``. Leading and trailing whitespace and dots are stripped
    to avoid hidden or extension-hostile names. The result is normalized to
    Unicode NFC form and truncated so its filesystem-encoded form does not
    exceed ``max_length`` bytes, matching the byte-oriented component limits
    enforced by common filesystems.

    Args:
        name: Candidate filename, which may contain unsafe characters.
        replacement: String substituted for each forbidden character. It
            must not itself contain a path separator or NUL byte.
        max_length: Maximum length of the returned filename in filesystem
            bytes. Must be positive.

    Returns:
        A sanitized filename containing no path separators or control
        characters.

    Raises:
        SecurityError: If ``name`` cannot yield a safe filename, if
            ``replacement`` is unsafe, or if ``max_length`` is not positive.
    """
    if max_length <= 0:
        raise SecurityError(
            "max_length must be a positive integer",
            operation="sanitize_filename",
            details={"max_length": max_length},
        )
    if any(_is_forbidden_char(char) for char in replacement):
        raise SecurityError(
            "replacement must not contain path separators or control characters",
            operation="sanitize_filename",
            details={"replacement": replacement},
        )

    normalized = unicodedata.normalize("NFC", name)
    sanitized = "".join(
        replacement if _is_forbidden_char(char) else char for char in normalized
    )
    sanitized = sanitized.strip().strip(".").strip()

    if not sanitized:
        raise SecurityError(
            "filename cannot be sanitized to a safe value",
            operation="sanitize_filename",
            file_path=name,
            details={"sanitized": sanitized},
        )

    if len(os.fsencode(sanitized)) > max_length:
        sanitized = _truncate_to_byte_length(sanitized, max_length).rstrip(". ")
        if not sanitized:
            raise SecurityError(
                "filename cannot be sanitized to a safe value",
                operation="sanitize_filename",
                file_path=name,
                details={"sanitized": sanitized},
            )

    return sanitized


def _truncate_to_byte_length(value: str, max_bytes: int) -> str:
    """Return ``value`` truncated so its encoded form fits within ``max_bytes``.

    Whole characters are dropped from the end so a multibyte character is
    never split across the byte limit, which mirrors how filesystems bound a
    path component in bytes rather than Unicode code points.

    Args:
        value: Filename component to truncate.
        max_bytes: Maximum length, in filesystem bytes, of the result.

    Returns:
        The longest prefix of ``value`` whose filesystem-encoded form does not
        exceed ``max_bytes`` bytes.
    """
    truncated = value
    while truncated and len(os.fsencode(truncated)) > max_bytes:
        truncated = truncated[:-1]
    return truncated
