"""The native EXIF original and its embedded XMP copy (#396, PR #486).

Orchestrator's ruling on PR #486: the camera's native EXIF
``DateTimeOriginal`` (with or without ``OffsetTimeOriginal``) is the
authority for the written local date and time. When a file carries both it
and an embedded XMP ``exif:DateTimeOriginal``, the XMP value is a copy: it
never competes for the value and never replaces the EXIF one. It is placed
relative to the EXIF value as follows.

1. Both zoned, spans overlap (same instant at the coarser precision, in
   either direction): compatible supporting evidence.
2. Both zoned, spans disjoint: a real contradiction; the decision abstains.
3. EXIF naive, XMP zoned: incomparable; the XMP neither wins nor lowers the
   EXIF's certainty.
4. EXIF zoned, XMP naive: compatible when the written readings overlap,
   otherwise incomparable (other digits give no zone mapping).
5. Both naive: compatible when the written readings overlap; a contradiction
   only when both are full time-of-day readings of the same precision;
   otherwise a hint.

Without a native EXIF original the XMP original dates the file under the
ordinary rules.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum, auto
from typing import Final

from winnow.media._date_policy import FieldKind
from winnow.media._date_screen import Screened
from winnow.media._date_spans import Agreement, compare, span_of, written_span
from winnow.models.evidence import Precision, SourceKind, TimeValue

_FULL_READINGS: Final[frozenset[Precision]] = frozenset(
    {Precision.MINUTE, Precision.SECOND, Precision.SUBSECOND},
)


class CopyPlace(StrEnum):
    """Where an XMP copy of the native EXIF original belongs."""

    COMPATIBLE = auto()
    INCOMPARABLE = auto()
    CONTRADICTS = auto()
    HINT = auto()


def _is_original(entry: Screened, source: SourceKind) -> bool:
    """Tell whether an eligible entry is a ``DateTimeOriginal`` from a source.

    Args:
        entry: A screened entry.
        source: ``exif`` or ``xmp``.

    Returns:
        Whether it reached a rule reading the original, from that source.
    """
    return (
        entry.rule is not None
        and entry.rule.kind is FieldKind.ORIGINAL
        and entry.observation.source is source
    )


def is_native_original(entry: Screened) -> bool:
    """Tell whether an entry is the camera's native EXIF original.

    Args:
        entry: A screened entry.

    Returns:
        Whether it is an eligible EXIF ``DateTimeOriginal``.
    """
    return _is_original(entry, SourceKind.EXIF)


def is_xmp_original(entry: Screened) -> bool:
    """Tell whether an entry is an embedded XMP ``exif:DateTimeOriginal``.

    Args:
        entry: A screened entry.

    Returns:
        Whether it is an eligible XMP ``DateTimeOriginal``.
    """
    return _is_original(entry, SourceKind.XMP)


def without_copies(eligible: list[Screened]) -> list[Screened]:
    """Drop XMP originals from the candidates when a native one exists.

    Args:
        eligible: The eligible entries.

    Returns:
        The entries that may supply the value.
    """
    if any(is_native_original(entry) for entry in eligible):
        return [entry for entry in eligible if not is_xmp_original(entry)]
    return eligible


def place_copy(native: TimeValue, copy: TimeValue) -> CopyPlace:
    """Place an XMP copy relative to the chosen native EXIF original.

    Args:
        native: The chosen EXIF value.
        copy: The XMP copy's value.

    Returns:
        Its place under cases 1 to 5 of the module docstring.
    """
    native_zoned = native.offset_minutes is not None
    if copy.offset_minutes is not None:
        if not native_zoned:
            return CopyPlace.INCOMPARABLE
        overlap = compare(span_of(native), span_of(copy), tolerance=timedelta(0))
        if overlap is Agreement.AGREE:
            return CopyPlace.COMPATIBLE
        return CopyPlace.CONTRADICTS
    return _place_naive_copy(native, copy, native_zoned=native_zoned)


def _place_naive_copy(
    native: TimeValue,
    copy: TimeValue,
    *,
    native_zoned: bool,
) -> CopyPlace:
    """Place an XMP copy without an offset (cases 4 and 5).

    Args:
        native: The chosen EXIF value.
        copy: The naive XMP copy's value.
        native_zoned: Whether the EXIF value has an offset.

    Returns:
        Compatible when the written readings overlap; otherwise incomparable
        beside a zoned EXIF value, a contradiction for like full readings,
        and a hint for the rest.
    """
    written = compare(written_span(native), written_span(copy), tolerance=timedelta(0))
    if written is Agreement.AGREE:
        return CopyPlace.COMPATIBLE
    if native_zoned:
        return CopyPlace.INCOMPARABLE
    if _like_readings(native, copy):
        return CopyPlace.CONTRADICTS
    return CopyPlace.HINT


def _like_readings(first: TimeValue, second: TimeValue) -> bool:
    """Tell whether two values are full readings at the same precision.

    Args:
        first: One value.
        second: The other.

    Returns:
        Whether both state a time of day at the same precision and fraction
        digits.
    """
    return (
        first.precision in _FULL_READINGS
        and first.precision is second.precision
        and first.fraction_digits == second.fraction_digits
    )
