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
    """Concatenate the ``APP11`` payloads of a JPEG file when they carry C2PA.

    A C2PA JUMBF box may span several contiguous ``APP11`` segments, and only
    the first fragment carries the ``jumb``/``c2pa`` markers. All ``APP11``
    payloads are therefore concatenated in file order before the markers are
    checked, so continuation fragments are kept.

    Args:
        path: Filesystem path to the JPEG.

    Returns:
        Concatenated segment payloads, or ``None`` when the file has no
        ``APP11`` segments or they do not contain a C2PA JUMBF box.
    """
    try:
        with Image.open(path) as image:
            applist: list[tuple[str, bytes]] = list(getattr(image, "applist", ()))
    except (OSError, UnidentifiedImageError, ValueError):
        return None
    manifest = b"".join(data for segment, data in applist if segment == "APP11")
    if _JUMBF_MARKER not in manifest or _C2PA_MARKER not in manifest:
        return None
    return manifest


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
