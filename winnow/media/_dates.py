"""Timestamp parsing helpers shared by the media metadata processors.

EXIF stores capture times as naive ``"YYYY:MM:DD HH:MM:SS"`` strings with no
timezone; ffprobe reports container creation times as ISO 8601 strings that
are usually UTC (``Z`` suffix). Both parsers map missing, placeholder, or
malformed values to ``None`` instead of raising.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

_EXIF_DATETIME_FORMAT: Final[str] = "%Y:%m:%d %H:%M:%S"
_UTC_SUFFIX: Final[str] = "Z"
_UTC_OFFSET: Final[str] = "+00:00"
_EXIF_PADDING: Final[str] = " \t\r\n\x00"


def parse_exif_datetime(value: str) -> datetime | None:
    """Parse an EXIF ``DateTime``-style string into a naive datetime.

    EXIF timestamps carry no timezone, and no timezone is guessed here; the
    result is naive and consumers decide how to interpret it. The EXIF
    all-zero placeholder (``"0000:00:00 00:00:00"``) is rejected because it
    has no valid calendar date. Surrounding whitespace and the NUL padding some
    cameras emit are ignored.

    Args:
        value: Raw EXIF tag value such as ``"2024:03:01 12:34:56"``.

    Returns:
        Naive datetime, or ``None`` when the value is empty, a placeholder, or
        not in ``YYYY:MM:DD HH:MM:SS`` format.
    """
    try:
        return datetime.strptime(value.strip(_EXIF_PADDING), _EXIF_DATETIME_FORMAT)
    except ValueError:
        return None


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp such as ffprobe's ``creation_time``.

    A trailing ``Z`` is normalised to ``+00:00`` so the result is timezone
    aware when the source carries an offset. Values without an offset parse
    to a naive datetime.

    Args:
        value: ISO 8601 timestamp, for example ``"2024-03-01T12:34:56.000000Z"``.

    Returns:
        Parsed datetime, or ``None`` when the value cannot be parsed.
    """
    normalised = value.strip()
    if normalised.endswith(_UTC_SUFFIX):
        normalised = normalised[: -len(_UTC_SUFFIX)] + _UTC_OFFSET
    try:
        return datetime.fromisoformat(normalised)
    except ValueError:
        return None
