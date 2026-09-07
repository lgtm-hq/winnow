"""Minimal C2PA manifest extraction for JPEG and PNG files.

C2PA (Coalition for Content Provenance and Authenticity) manifests are stored in
JUMBF boxes: JPEG files carry them in ``APP11`` segments and PNG files in
``caBX`` chunks. This module locates those containers and returns their raw
bytes; it does not parse the manifest structure.

The only interpretation offered is :func:`manifest_declares_ai_source`, a
substring search for the C2PA ``digitalSourceType`` values that mark
generative-AI output. Bare manifest presence is not evidence of AI generation:
cameras and desktop editors write C2PA manifests too.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, UnidentifiedImageError

AI_SOURCE_TYPE_MARKERS: frozenset[bytes] = frozenset(
    {
        b"trainedAlgorithmicMedia",
        b"compositeWithTrainedAlgorithmicMedia",
    },
)
"""IPTC ``digitalSourceType`` suffixes that C2PA uses for generative-AI output."""

_JPEG_SUFFIXES = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})
_PNG_SUFFIXES = frozenset({".png"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_C2PA_CHUNK = b"caBX"
_PNG_CHUNK_HEADER = struct.Struct(">I4s")
_PNG_CRC_LENGTH = 4
_JUMBF_MARKER = b"jumb"
_C2PA_MARKER = b"c2pa"
# JPEG APP11 JUMBF segments start with the common identifier ``JP``, a 2-byte
# box instance number (``En``) and a 4-byte packet sequence number (``Z``).
_APP11_COMMON_IDENTIFIER = b"JP"
_APP11_INSTANCE_SLICE = slice(2, 4)
_APP11_HEADER_LENGTH = 8


def read_c2pa_manifest(path: Path) -> bytes | None:
    """Return the raw C2PA manifest bytes embedded in a JPEG or PNG file.

    JPEG manifests are the concatenated payloads of every ``APP11`` segment
    that contains both ``b"jumb"`` and ``b"c2pa"``. PNG manifests are the
    concatenated data of every ``caBX`` chunk. Other formats, files without a
    manifest, and unreadable files all yield ``None``; this function never
    raises for a readable file.

    Args:
        path: Filesystem path to the image.

    Returns:
        The manifest bytes, or ``None`` when absent or unsupported.
    """
    suffix = path.suffix.casefold()
    if suffix in _JPEG_SUFFIXES:
        return _read_jpeg_manifest(path)
    if suffix in _PNG_SUFFIXES:
        return _read_png_manifest(path)
    return None


def manifest_declares_ai_source(manifest: bytes) -> bool:
    """Return whether a C2PA manifest declares a generative-AI source type.

    Args:
        manifest: Raw manifest bytes from :func:`read_c2pa_manifest`.

    Returns:
        ``True`` when any :data:`AI_SOURCE_TYPE_MARKERS` byte string occurs in
        the manifest.
    """
    return any(marker in manifest for marker in AI_SOURCE_TYPE_MARKERS)


def _read_jpeg_manifest(path: Path) -> bytes | None:
    """Reassemble the C2PA JUMBF ``APP11`` boxes of a JPEG file.

    A JUMBF box may span several ``APP11`` segments that share a box instance
    number; only the first fragment carries the ``jumb``/``c2pa`` markers.
    Segments are grouped by instance number in file order, and every group
    whose reassembled payload contains both markers is kept. ``APP11``
    segments that are not JUMBF, or belong to a box without the markers, are
    ignored.

    Args:
        path: Filesystem path to the JPEG.

    Returns:
        Concatenated payloads of the qualifying boxes, or ``None`` when the
        file carries no C2PA JUMBF box.
    """
    try:
        with Image.open(path) as image:
            applist: list[tuple[str, bytes]] = list(getattr(image, "applist", ()))
    except (OSError, UnidentifiedImageError, ValueError):
        return None
    boxes: dict[bytes, list[bytes]] = {}
    for segment, data in applist:
        if segment != "APP11" or not data.startswith(_APP11_COMMON_IDENTIFIER):
            continue
        boxes.setdefault(data[_APP11_INSTANCE_SLICE], []).append(
            data[_APP11_HEADER_LENGTH:],
        )
    payloads = [
        payload
        for fragments in boxes.values()
        for payload in (b"".join(fragments),)
        if _JUMBF_MARKER in payload and _C2PA_MARKER in payload
    ]
    return b"".join(payloads) if payloads else None


def _read_png_manifest(path: Path) -> bytes | None:
    """Concatenate the ``caBX`` chunk data of a PNG file.

    Args:
        path: Filesystem path to the PNG.

    Returns:
        Concatenated chunk data, or ``None`` when the file has no ``caBX``
        chunk or is not a well-formed PNG.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(_PNG_SIGNATURE):
        return None

    payloads: list[bytes] = []
    offset = len(_PNG_SIGNATURE)
    while offset + _PNG_CHUNK_HEADER.size <= len(data):
        length, chunk_type = _PNG_CHUNK_HEADER.unpack_from(data, offset)
        start = offset + _PNG_CHUNK_HEADER.size
        end = start + length
        if end + _PNG_CRC_LENGTH > len(data):
            break
        if chunk_type == _PNG_C2PA_CHUNK:
            payloads.append(data[start:end])
        offset = end + _PNG_CRC_LENGTH
    return b"".join(payloads) if payloads else None
