"""Builders for tiny hand-made ISO-BMFF and QuickTime files used in tests."""

from __future__ import annotations

import struct
from collections.abc import Sequence
from pathlib import Path

from winnow.media._detect import CANON_UUID, TRACK_BOX


def box(kind: bytes, payload: bytes = b"") -> bytes:
    """Build a box with a 32-bit size.

    Args:
        kind: Four-byte box type.
        payload: Box payload.

    Returns:
        The encoded box.
    """
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def large_box(kind: bytes, payload: bytes = b"") -> bytes:
    """Build a box with a 64-bit (extended) size.

    Args:
        kind: Four-byte box type.
        payload: Box payload.

    Returns:
        The encoded box.
    """
    return struct.pack(">I", 1) + kind + struct.pack(">Q", 16 + len(payload)) + payload


def brands(names: Sequence[str]) -> bytes:
    """Encode brands, four bytes each.

    Args:
        names: Brands, spaces included.

    Returns:
        The concatenated brand bytes.

    Raises:
        ValueError: If a brand is not four bytes long.
    """
    encoded = b"".join(name.encode("latin-1") for name in names)
    if len(encoded) % 4:
        raise ValueError(f"brands must be four bytes each: {list(names)!r}")
    return encoded


def ftyp(major: str, compatible: Sequence[str] = ()) -> bytes:
    """Build an ``ftyp`` box.

    Args:
        major: Major brand.
        compatible: Compatible brands in order.

    Returns:
        The encoded box.
    """
    return box(b"ftyp", brands([major]) + b"\x00\x00\x00\x00" + brands(compatible))


def hdlr(handler: str) -> bytes:
    """Build an ``hdlr`` box for ``handler``.

    Args:
        handler: Handler type such as ``"vide"`` or ``"soun"``.

    Returns:
        The encoded box.
    """
    payload = b"\x00" * 8 + brands([handler]) + b"\x00" * 12 + b"\x00"
    return box(b"hdlr", payload)


def track(handler: str) -> bytes:
    """Build a track box holding ``mdia/hdlr``.

    Args:
        handler: Handler type.

    Returns:
        The encoded box.
    """
    return box(TRACK_BOX, box(b"tkhd", b"\x00" * 8) + box(b"mdia", hdlr(handler)))


def canon_uuid(profile: bytes) -> bytes:
    """Build Canon's ``moov/uuid`` box holding a ``CNCV`` profile.

    Args:
        profile: ``CNCV`` payload, such as ``b"CanonCR3_001/00.09.00/00.00.00"``.

    Returns:
        The encoded box.
    """
    return box(b"uuid", CANON_UUID + box(b"CNCV", profile) + box(b"CCTP", b"\x00" * 8))


def moov(handlers: Sequence[str] = ("vide",), *, extra: bytes = b"") -> bytes:
    """Build a ``moov`` with one track per handler.

    Args:
        handlers: Handler type of each track, in order.
        extra: Further child boxes placed before the tracks.

    Returns:
        The encoded box.
    """
    body = box(b"mvhd", b"\x00" * 16) + extra
    body += b"".join(track(handler) for handler in handlers)
    return box(b"moov", body)


def write_ftyp(
    path: Path,
    major: str,
    compatible: Sequence[str],
    tracks: Sequence[str] | None = ("vide",),
) -> Path:
    """Write ``ftyp`` plus a minimal ``moov/trak/mdia/hdlr`` to ``path``.

    Args:
        path: Destination file.
        major: Major brand.
        compatible: Compatible brands.
        tracks: Handler of each track; ``None`` writes no ``moov``.

    Returns:
        ``path``.
    """
    data = ftyp(major, compatible)
    if tracks is not None:
        data += moov(tracks)
    data += box(b"mdat", b"\x00" * 16)
    path.write_bytes(data)
    return path
