"""Shared numeric coercion helpers for media metadata processors.

Backend probes (mutagen, tinytag, ffprobe) surface loosely typed numeric
fields. These helpers normalise such values into non-negative numbers,
mapping missing or invalid inputs to ``None`` instead of raising.
"""

from __future__ import annotations


def coerce_non_negative_int(value: object, *, via_float: bool = False) -> int | None:
    """Coerce a value into a non-negative integer.

    Args:
        value: Raw backend field value.
        via_float: When ``True``, parse through ``float`` first so decimal
            strings such as ``"12.5"`` truncate to an integer (ffprobe
            semantics). When ``False``, such strings are rejected as
            invalid (mutagen/tinytag semantics).

    Returns:
        Parsed integer, or ``None`` when missing, invalid, or negative.
    """
    if value is None:
        return None
    try:
        if via_float:
            parsed = int(float(value))  # type: ignore[arg-type]
        else:
            parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def coerce_non_negative_float(value: object) -> float | None:
    """Coerce a value into a non-negative float.

    Args:
        value: Raw backend field value.

    Returns:
        Parsed float, or ``None`` when missing, invalid, or negative.
    """
    if value is None:
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
