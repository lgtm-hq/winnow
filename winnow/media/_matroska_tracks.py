"""Native Matroska track types for detection rule 3 (#393).

After the EBML header, a Matroska file is one ``Segment`` element whose
top-level children include ``Tracks``, holding one ``TrackEntry`` per track
with its ``TrackType``. This module walks the Segment's top-level elements
with a bounded reader, the way rule 2 reads ``hdlr`` boxes: it reads
element headers and seeks past payloads, never reads a ``Cluster`` payload,
and follows the ``SeekHead`` to ``Tracks`` when ``Tracks`` comes after the
first ``Cluster``.

Specifications:

* RFC 8794 (EBML), section 4: element IDs and data sizes are variable-size
  integers (VINT). The number of leading zero bits of the first byte, plus
  one, is the integer's length. An ID keeps its marker bit; a data size
  drops it, and a size whose value bits are all set is unknown (section
  6.2), allowed only for master elements.
* RFC 9559 (Matroska), section 5.1: element IDs ``Segment`` 0x18538067,
  ``SeekHead`` 0x114D9B74, ``Seek`` 0x4DBB, ``SeekID`` 0x53AB,
  ``SeekPosition`` 0x53AC, ``Cluster`` 0x1F43B675, ``Tracks`` 0x1654AE6B,
  ``TrackEntry`` 0xAE, ``TrackType`` 0x83. ``SeekPosition`` counts from the
  start of the Segment's data (5.1.1.1.2). ``TrackType`` 1 is video, 2 is
  audio, 3 a complex track mixing both (5.1.4.1.3). Section 6.3: the first
  ``Tracks`` either comes before the first ``Cluster`` or is referenced by a
  ``SeekHead`` before it, so the walk stops at the first ``Cluster``.

Any video track gives video. Otherwise at least one audio track gives
audio, decided only after the whole ``Tracks`` element is read. A complex
track without a video track, a missing ``Tracks``, malformed input or a
walk over budget does not settle the type; the caller then asks ffprobe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from winnow.media._detect_base import BudgetExceededError, MalformedError, Reader
from winnow.models.media import MediaType

MAX_BYTES_READ: Final[int] = 256 * 1024
"""Bytes the track walk may read: element headers and the master payloads."""

MAX_ELEMENTS: Final[int] = 4096
"""Element headers the track walk may visit, at all depths together."""

MAX_DEPTH: Final[int] = 4
"""Deepest nesting the walk reads: Segment, Tracks, TrackEntry, TrackType."""

MAX_MASTER_BYTES: Final[int] = 64 * 1024
"""Largest ``Tracks`` or ``SeekHead`` payload read; larger is over budget."""

SEGMENT: Final[int] = 0x18538067
SEEK_HEAD: Final[int] = 0x114D9B74
SEEK: Final[int] = 0x4DBB
SEEK_ID: Final[int] = 0x53AB
SEEK_POSITION: Final[int] = 0x53AC
CLUSTER: Final[int] = 0x1F43B675
TRACKS: Final[int] = 0x1654AE6B
TRACK_ENTRY: Final[int] = 0xAE
TRACK_TYPE: Final[int] = 0x83

VIDEO_TRACK: Final[int] = 1
AUDIO_TRACK: Final[int] = 2
COMPLEX_TRACK: Final[int] = 3

_TRACK_NAMES: Final[dict[int, str]] = {
    VIDEO_TRACK: "video",
    AUDIO_TRACK: "audio",
    COMPLEX_TRACK: "complex",
    0x10: "logo",
    0x11: "subtitle",
    0x12: "buttons",
    0x20: "control",
    0x21: "metadata",
}
_HEADER_READ: Final[int] = 12
_MAX_ID_BYTES: Final[int] = 4
_MAX_SIZE_BYTES: Final[int] = 8


@dataclass(frozen=True, slots=True)
class TrackWalk:
    """What the native walk established.

    Args:
        media_type: Video, audio, or ``None`` when not settled.
        settled: Whether the walk read a whole ``Tracks`` element and needs
            no ffprobe; ``False`` when it could not.
        detail: The track types found, or why the walk could not settle.
    """

    media_type: MediaType | None
    settled: bool
    detail: str


@dataclass(frozen=True, slots=True)
class _Element:
    """One element header.

    Args:
        element_id: Element ID, marker bit kept.
        offset: File offset of the element.
        data: File offset of its data.
        end: File offset just past it; ``None`` for an unknown size.
    """

    element_id: int
    offset: int
    data: int
    end: int | None


def walk_tracks(reader: Reader, segment_offset: int) -> TrackWalk:
    """Read the track types of the Segment at ``segment_offset``.

    Args:
        reader: Reader over the open file; its budget bounds the walk.
        segment_offset: File offset just past the EBML header.

    Returns:
        The track walk's answer; never raises for malformed input.
    """
    try:
        types = _find_track_types(reader, segment_offset)
    except MalformedError as exc:
        return TrackWalk(None, False, f"tracks=unresolved malformed: {exc}")
    except BudgetExceededError as exc:
        return TrackWalk(None, False, f"tracks=unresolved budget-exceeded: {exc}")
    if types is None:
        return TrackWalk(None, False, "tracks=unresolved: no Tracks before a Cluster")
    return _classify(types)


def _classify(types: list[int]) -> TrackWalk:
    """Decide from the track types of a whole ``Tracks`` element.

    Args:
        types: Every ``TrackType``, in file order.

    Returns:
        Video for any video track; unsettled for a complex track without
        one; audio for at least one audio track; otherwise settled unknown.
    """
    names = ",".join(_TRACK_NAMES.get(kind, f"type-{kind}") for kind in types)
    detail = f"tracks={names or '(none)'}"
    if VIDEO_TRACK in types:
        return TrackWalk(MediaType.VIDEO, True, detail)
    if COMPLEX_TRACK in types:
        return TrackWalk(None, False, f"{detail} unresolved: complex track")
    if AUDIO_TRACK in types:
        return TrackWalk(MediaType.AUDIO, True, detail)
    return TrackWalk(None, True, detail)


def _find_track_types(reader: Reader, segment_offset: int) -> list[int] | None:
    """Walk the Segment's top-level elements to every ``Tracks``.

    Every ``Tracks`` before the first ``Cluster`` is read and their track
    types joined. ``Tracks`` and ``SeekHead`` must lie inside the Segment.

    Args:
        reader: Reader over the open file.
        segment_offset: File offset of the Segment element.

    Returns:
        The track types, or ``None`` when no ``Tracks`` comes before the
        first ``Cluster`` and no ``SeekHead`` points to one.

    Raises:
        MalformedError: On an invalid header, element or seek position.
    """
    segment = _read_header(reader, segment_offset, end=reader.size)
    if segment.element_id != SEGMENT:
        raise MalformedError("no Segment after the EBML header")
    end = reader.size if segment.end is None else min(segment.end, reader.size)
    types, seek_tracks = _walk_segment(reader, segment.data, end)
    if types is not None:
        return types
    if seek_tracks is None:
        return None
    if not segment.data <= seek_tracks < end:
        raise MalformedError("SeekHead points outside the Segment")
    element = _read_header(reader, seek_tracks, end=end)
    if element.element_id != TRACKS:
        raise MalformedError("SeekHead position is not a Tracks element")
    return _read_tracks(reader, _inside(element, end))


def _walk_segment(
    reader: Reader,
    start: int,
    end: int,
) -> tuple[list[int] | None, int | None]:
    """Read every ``Tracks`` and the first ``SeekHead`` before a Cluster.

    Args:
        reader: Reader over the open file.
        start: File offset of the Segment's data.
        end: File offset just past the Segment, or the file's end.

    Returns:
        The joined track types (``None`` without a ``Tracks``) and the
        ``Tracks`` position the ``SeekHead`` gives, if any.

    Raises:
        MalformedError: On an element of unknown size other than a Cluster.
    """
    seek_tracks: int | None = None
    types: list[int] | None = None
    offset = start
    while offset < end:
        element = _read_header(reader, offset, end=end)
        if element.element_id == CLUSTER:
            break
        if element.end is None:
            raise MalformedError("unknown size outside Segment and Cluster")
        if element.element_id == TRACKS:
            types = [*(types or []), *_read_tracks(reader, _inside(element, end))]
        elif element.element_id == SEEK_HEAD and seek_tracks is None:
            seek_tracks = _read_seek_head(reader, _inside(element, end), start)
        offset = element.end
    return types, seek_tracks


def _inside(element: _Element, end: int) -> _Element:
    """Check that a master element lies inside the Segment.

    Args:
        element: The element.
        end: File offset just past the Segment, or the file's end.

    Returns:
        ``element``.

    Raises:
        MalformedError: When it has an unknown size or passes ``end``.
    """
    if element.end is None or element.end > end:
        raise MalformedError("master element extends past the Segment")
    return element


def _read_seek_head(reader: Reader, element: _Element, segment_data: int) -> int | None:
    """Return the file offset the ``SeekHead`` gives for ``Tracks``.

    Args:
        reader: Reader over the open file.
        element: The ``SeekHead`` element.
        segment_data: File offset of the Segment's data.

    Returns:
        The offset, or ``None`` when the ``SeekHead`` lists no ``Tracks``.
    """
    payload = _read_master(reader, element)
    for seek, seek_data in _children(reader, payload, element.data, depth=2):
        if seek.element_id != SEEK:
            continue
        target_id: int | None = None
        position: int | None = None
        for child, value in _children(reader, seek_data, seek.data, depth=3):
            if child.element_id == SEEK_ID:
                target_id = int.from_bytes(value, "big")
            elif child.element_id == SEEK_POSITION:
                position = int.from_bytes(value, "big")
        if target_id == TRACKS and position is not None:
            return segment_data + position
    return None


def _read_tracks(reader: Reader, element: _Element) -> list[int]:
    """Read the ``TrackType`` of every ``TrackEntry`` in ``Tracks``.

    Args:
        reader: Reader over the open file.
        element: The ``Tracks`` element.

    Returns:
        The track types in file order.

    Raises:
        MalformedError: When a ``TrackEntry`` has no valid ``TrackType``.
    """
    payload = _read_master(reader, element)
    types: list[int] = []
    for entry, entry_data in _children(reader, payload, element.data, depth=2):
        if entry.element_id != TRACK_ENTRY:
            continue
        kind: int | None = None
        for child, value in _children(reader, entry_data, entry.data, depth=3):
            if child.element_id == TRACK_TYPE:
                if not 1 <= len(value) <= _MAX_SIZE_BYTES:
                    raise MalformedError("TrackType is not an unsigned integer")
                kind = int.from_bytes(value, "big")
        if kind is None:
            raise MalformedError("TrackEntry without TrackType")
        types.append(kind)
    return types


def _read_master(reader: Reader, element: _Element) -> bytes:
    """Read the whole data of a master element.

    Args:
        reader: Reader over the open file.
        element: Master element.

    Returns:
        Its data bytes.

    Raises:
        MalformedError: On an unknown size or data cut by the file.
        BudgetExceededError: When the data is over :data:`MAX_MASTER_BYTES`.
    """
    if element.end is None:
        raise MalformedError("master element of unknown size")
    length = element.end - element.data
    if length > MAX_MASTER_BYTES:
        raise BudgetExceededError("master element larger than its read budget")
    return reader.read_exact(element.data, length, "master element")


def _children(
    reader: Reader,
    data: bytes,
    base: int,
    *,
    depth: int,
) -> list[tuple[_Element, bytes]]:
    """Parse the child elements in a parent's data.

    Args:
        reader: Reader whose element count is charged.
        data: The parent's data bytes.
        base: File offset of ``data[0]``.
        depth: Depth of the children (the Segment is 0).

    Returns:
        Each child header with its own data bytes.

    Raises:
        MalformedError: On a child of unknown size or past its parent.
        BudgetExceededError: Past :data:`MAX_DEPTH` or :data:`MAX_ELEMENTS`.
    """
    if depth > MAX_DEPTH:
        raise BudgetExceededError("element depth")
    children: list[tuple[_Element, bytes]] = []
    position = 0
    while position < len(data):
        _count(reader)
        element = _parse_header(
            data[position : position + _HEADER_READ], base + position
        )
        if element.end is None:
            raise MalformedError("child element of unknown size")
        if element.end > base + len(data):
            raise MalformedError("element extends past its parent")
        children.append((element, data[element.data - base : element.end - base]))
        position = element.end - base
    return children


def _read_header(reader: Reader, offset: int, *, end: int) -> _Element:
    """Read the element header at ``offset``.

    Args:
        reader: Reader over the open file.
        offset: File offset of the element.
        end: Offset the header may not be read past.

    Returns:
        The header. A size reaching past ``end`` is not an error here: a
        skipped element ends the walk, and a master element read in full is
        checked by :meth:`Reader.read_exact`.
    """
    _count(reader)
    return _parse_header(
        reader.read_at(offset, min(_HEADER_READ, end - offset)), offset
    )


def _count(reader: Reader) -> None:
    """Charge one element header against :data:`MAX_ELEMENTS`.

    Args:
        reader: Reader whose count is charged.

    Raises:
        BudgetExceededError: Past :data:`MAX_ELEMENTS`.
    """
    reader.boxes += 1
    if reader.boxes > MAX_ELEMENTS:
        raise BudgetExceededError("element count")


def _parse_header(raw: bytes, offset: int) -> _Element:
    """Parse an element ID and data size (RFC 8794, sections 4 and 5).

    Args:
        raw: Bytes starting at the element.
        offset: File offset of ``raw[0]``.

    Returns:
        The header; ``end`` is ``None`` for an unknown data size.

    Raises:
        MalformedError: On a truncated or invalid ID or size.
    """
    id_width = _vint_width(raw, 0, _MAX_ID_BYTES, "element ID")
    element_id = int.from_bytes(raw[:id_width], "big")
    size_width = _vint_width(raw, id_width, _MAX_SIZE_BYTES, "data size")
    size = int.from_bytes(raw[id_width : id_width + size_width], "big")
    size &= (1 << (7 * size_width)) - 1
    data = offset + id_width + size_width
    unknown = size == (1 << (7 * size_width)) - 1
    return _Element(element_id, offset, data, None if unknown else data + size)


def _vint_width(raw: bytes, position: int, limit: int, what: str) -> int:
    """Return the length of the VINT at ``position`` (RFC 8794, 4.1).

    Args:
        raw: Bytes read.
        position: Offset of the VINT's first byte.
        limit: Longest length allowed.
        what: Name of the field, for the error message.

    Returns:
        The VINT's length in bytes.

    Raises:
        MalformedError: On a zero first byte, a length over ``limit`` or a
            VINT cut by the bytes read.
    """
    if position >= len(raw) or raw[position] == 0:
        raise MalformedError(f"truncated or invalid {what}")
    width = 9 - raw[position].bit_length()
    if width > limit or position + width > len(raw):
        raise MalformedError(f"truncated or invalid {what}")
    return width


__all__ = [
    "AUDIO_TRACK",
    "CLUSTER",
    "COMPLEX_TRACK",
    "MAX_BYTES_READ",
    "MAX_DEPTH",
    "MAX_ELEMENTS",
    "MAX_MASTER_BYTES",
    "SEEK",
    "SEEK_HEAD",
    "SEEK_ID",
    "SEEK_POSITION",
    "SEGMENT",
    "TRACKS",
    "TRACK_ENTRY",
    "TRACK_TYPE",
    "VIDEO_TRACK",
    "TrackWalk",
    "walk_tracks",
]
