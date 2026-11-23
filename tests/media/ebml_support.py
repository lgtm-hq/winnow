"""Builders for tiny Matroska and WebM files used in detection tests.

IDs and the variable-size integer coding follow RFC 8794 (EBML) and RFC 9559
(Matroska); see :mod:`winnow.media._matroska_tracks`.
"""

from __future__ import annotations

UNKNOWN_SIZE = b"\x01\xff\xff\xff\xff\xff\xff\xff"
"""An eight-byte data size with every value bit set: unknown."""

EBML = bytes.fromhex("1a45dfa3")
SEGMENT = bytes.fromhex("18538067")
SEEK_HEAD = bytes.fromhex("114d9b74")
SEEK = bytes.fromhex("4dbb")
SEEK_ID = bytes.fromhex("53ab")
SEEK_POSITION = bytes.fromhex("53ac")
INFO = bytes.fromhex("1549a966")
TRACKS = bytes.fromhex("1654ae6b")
TRACK_ENTRY = bytes.fromhex("ae")
TRACK_NUMBER = bytes.fromhex("d7")
TRACK_TYPE = bytes.fromhex("83")
CLUSTER = bytes.fromhex("1f43b675")
VOID = bytes.fromhex("ec")

VIDEO = 1
AUDIO = 2
COMPLEX = 3
SUBTITLE = 0x11


def size(value: int) -> bytes:
    """Encode a data size as the shortest VINT (RFC 8794, 4).

    Args:
        value: The size.

    Returns:
        The encoded size.
    """
    width = 1
    while value >= (1 << (7 * width)) - 1:
        width += 1
    return ((1 << (7 * width)) | value).to_bytes(width, "big")


def element(element_id: bytes, payload: bytes = b"") -> bytes:
    """Build one element with a known size.

    Args:
        element_id: ID bytes, marker bit included.
        payload: Data bytes.

    Returns:
        The element.
    """
    return element_id + size(len(payload)) + payload


def ebml_header(doctype: bytes = b"matroska") -> bytes:
    """Build an EBML header with a DocType.

    Args:
        doctype: ``matroska`` or ``webm``.

    Returns:
        The header element.
    """
    return element(EBML, element(bytes.fromhex("4282"), doctype))


def track_entry(kind: int | None, number: int = 1) -> bytes:
    """Build a ``TrackEntry``.

    Args:
        kind: ``TrackType``, or ``None`` to leave it out.
        number: ``TrackNumber``.

    Returns:
        The element.
    """
    body = element(TRACK_NUMBER, bytes([number]))
    if kind is not None:
        body += element(TRACK_TYPE, bytes([kind]))
    return element(TRACK_ENTRY, body)


def tracks(*kinds: int | None) -> bytes:
    """Build ``Tracks`` with one entry per track type.

    Args:
        kinds: Track types.

    Returns:
        The element.
    """
    return element(TRACKS, b"".join(track_entry(k, n + 1) for n, k in enumerate(kinds)))


def seek_head(target: bytes, position: int) -> bytes:
    """Build a ``SeekHead`` with one ``Seek``.

    Args:
        target: ID bytes of the element sought.
        position: Its position from the start of the Segment's data.

    Returns:
        The element.
    """
    seek = element(SEEK_ID, target) + element(
        SEEK_POSITION, position.to_bytes(4, "big")
    )
    return element(SEEK_HEAD, element(SEEK, seek))


def cluster(payload_bytes: int = 32, *, unknown: bool = False) -> bytes:
    """Build a ``Cluster`` of zero bytes.

    Args:
        payload_bytes: Payload length.
        unknown: Write an unknown size, as live streams do.

    Returns:
        The element.
    """
    payload = b"\x00" * payload_bytes
    if unknown:
        return CLUSTER + UNKNOWN_SIZE + payload
    return element(CLUSTER, payload)


def segment(*children: bytes, unknown: bool = False) -> bytes:
    """Build a ``Segment``.

    Args:
        children: Top-level elements.
        unknown: Write an unknown size, as live streams do.

    Returns:
        The element.
    """
    body = b"".join(children)
    if unknown:
        return SEGMENT + UNKNOWN_SIZE + body
    return element(SEGMENT, body)


def matroska(
    *children: bytes, doctype: bytes = b"matroska", unknown: bool = False
) -> bytes:
    """Build a whole file: EBML header and Segment.

    Args:
        children: The Segment's top-level elements.
        doctype: DocType.
        unknown: Give the Segment an unknown size.

    Returns:
        The file bytes.
    """
    return ebml_header(doctype) + segment(*children, unknown=unknown)


def tracks_after_cluster(cluster_bytes: int) -> bytes:
    """Build a file whose ``Tracks`` follows a large ``Cluster``.

    A ``SeekHead`` before the ``Cluster`` gives the ``Tracks`` position.

    Args:
        cluster_bytes: Payload length of the ``Cluster``.

    Returns:
        The file bytes.
    """
    info = element(INFO, b"\x00" * 8)
    head_length = len(seek_head(TRACKS, 0))
    big = cluster(cluster_bytes)
    position = head_length + len(info) + len(big)
    return matroska(seek_head(TRACKS, position), info, big, tracks(AUDIO))
