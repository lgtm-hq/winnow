"""Name the kind of date field an observation is (#396).

Rule matching reads the observation's claim, source and locator. Locators
are written by the readers (#395, #402); the resolver matches the field
names they contain case-insensitively, not their exact spelling, so a
reader may add a ``#<n>`` occurrence counter or a namespace URI without
changing the rule that reads the field.
"""

from __future__ import annotations

from typing import Final

from winnow.media._date_policy import FieldKind
from winnow.models.evidence import Claim, Observation, SourceKind

_NAMED_FIELDS: Final[dict[SourceKind, tuple[str, FieldKind]]] = {
    SourceKind.IPTC: ("", FieldKind.DATE_CREATED),
    SourceKind.QUICKTIME: (
        "com.apple.quicktime.creationdate",
        FieldKind.APPLE_CREATIONDATE,
    ),
    SourceKind.SIDECAR_TAKEOUT: ("phototakentime", FieldKind.TAKEOUT),
    SourceKind.PNG_TEXT: ("creation time", FieldKind.PNG_CREATION),
    SourceKind.FILENAME: ("", FieldKind.FILENAME),
}
"""Sources whose capture field is named by one lower-case locator fragment
(empty: every capture observation from that source)."""


def field_kind(observation: Observation) -> FieldKind | None:
    """Name the kind of date field an observation is.

    Args:
        observation: A capture or digitized observation.

    Returns:
        The field kind, or ``None`` for a field no rule reads.
    """
    if observation.claim is Claim.DIGITIZED_TIME:
        if observation.source in {SourceKind.EXIF, SourceKind.XMP}:
            return FieldKind.DIGITIZED
        return None
    locator = observation.locator.lower()
    source = observation.source
    if source in {SourceKind.EXIF, SourceKind.XMP}:
        return _embedded_kind(source, locator)
    if source is SourceKind.SIDECAR_XMP:
        return _xmp_kind(locator, original=FieldKind.SIDECAR_XMP)
    match = _NAMED_FIELDS.get(source)
    if match is None or match[0] not in locator:
        return None
    return match[1]


def _embedded_kind(source: SourceKind, locator: str) -> FieldKind | None:
    """Name an embedded EXIF or XMP capture field.

    Args:
        source: ``exif`` or ``xmp``.
        locator: The lower-cased locator.

    Returns:
        GPS for an EXIF GPS field, otherwise the XMP-style field kind.
    """
    if source is SourceKind.EXIF:
        if "gps" in locator:
            return FieldKind.GPS
        if "datetimeoriginal" in locator:
            return FieldKind.ORIGINAL
        return None
    return _xmp_kind(locator, original=FieldKind.ORIGINAL)


def _xmp_kind(locator: str, *, original: FieldKind) -> FieldKind | None:
    """Name an XMP capture field.

    Args:
        locator: The lower-cased locator.
        original: The kind to give ``DateTimeOriginal`` and, for a sidecar,
            ``DateCreated``.

    Returns:
        The field kind, or ``None``.
    """
    if "datetimeoriginal" in locator:
        return original
    if "datecreated" in locator:
        if original is FieldKind.SIDECAR_XMP:
            return original
        return FieldKind.DATE_CREATED
    return None
