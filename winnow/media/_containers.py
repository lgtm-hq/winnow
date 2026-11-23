"""Stream-type walks of multi-stream containers, for rule 4 (#393).

A container's magic says what it is, not whether it holds video. For each
container, a bounded walk reads the stream declarations the format puts
before its media data. Each walk cites its specification and reports
whether it read the whole declaration list:

* any video stream gives video, even from a partial list;
* audio is decided only from a complete list in which every stream is
  recognised and at least one is audio;
* anything else, including a walk cut short by the bytes read, by
  :data:`MAX_CHUNKS` or by :data:`MAX_DEPTH`, gives no answer, and the
  caller asks ffprobe.

This module holds the shared types and the AVI, ASF and RealMedia walks;
:mod:`winnow.media._packet_streams` holds FLV, MPEG-PS and Ogg. Every walk
is bounded by one :class:`WalkBudget`: a single count of chunks visited at
all depths, and a nesting depth cap, so no input can recurse without end.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from typing import Final

from winnow.media._detect_base import Content
from winnow.models.media import MediaType

VIDEO: Final[str] = "video"
AUDIO: Final[str] = "audio"
DATA: Final[str] = "data"

MAX_CHUNKS: Final[int] = 1024
"""Chunks, objects, tags or packets a walk may visit, at all depths."""

MAX_DEPTH: Final[int] = 4
"""Deepest nesting a walk descends into; deeper makes the list incomplete."""

StreamKinds = list[str | None]
"""Kinds of the streams a walk found; ``None`` for an unrecognised one."""


@dataclass(slots=True)
class StreamList:
    """The streams a walk found.

    Args:
        kinds: Each stream's kind, ``None`` for an unrecognised one.
        complete: Whether the walk read the whole declaration list.
    """

    kinds: StreamKinds = field(default_factory=list)
    complete: bool = False


@dataclass(slots=True)
class WalkBudget:
    """One element count shared by every depth of a walk.

    Args:
        visited: Elements visited so far.
    """

    visited: int = 0

    def visit(self) -> bool:
        """Count one element.

        Returns:
            ``False`` once :data:`MAX_CHUNKS` is passed.
        """
        self.visited += 1
        return self.visited <= MAX_CHUNKS


def media_type_of(streams: StreamList) -> MediaType | None:
    """Decide a container's media type from its streams.

    Args:
        streams: What the walk found.

    Returns:
        Video when any stream is video; audio when the list is complete,
        at least one stream is audio and every stream is recognised;
        otherwise ``None``.
    """
    if VIDEO in streams.kinds:
        return MediaType.VIDEO
    if streams.complete and AUDIO in streams.kinds and None not in streams.kinds:
        return MediaType.AUDIO
    return None


# --- AVI (Microsoft AVI RIFF File Reference) --------------------------------

_AVI_STREAM_TYPES: Final[dict[bytes, str]] = {
    b"vids": VIDEO,
    b"auds": AUDIO,
    b"iavs": VIDEO,  # interleaved DV audio and video
    b"txts": DATA,
    b"mids": DATA,
}
_RIFF_HEADER: Final[int] = 12
_CHUNK_HEADER: Final[int] = 8
_FOURCC: Final[int] = 4


def avi_streams(content: Content) -> StreamList:
    """Read the ``strh`` stream types in an AVI's ``hdrl`` list.

    The list is complete when the whole ``LIST hdrl`` lies in the bytes
    read and every chunk inside it is whole.

    Args:
        content: File content, ``RIFF....AVI `` first.

    Returns:
        The streams in file order.
    """
    head = content.head
    streams = StreamList()
    budget = WalkBudget()
    offset = _RIFF_HEADER
    while offset + _RIFF_HEADER <= len(head) and budget.visit():
        chunk, length = _riff_chunk(head, offset)
        body = offset + _CHUNK_HEADER
        if chunk == b"LIST" and head[body : body + _FOURCC] == b"hdrl":
            end = body + length
            whole = _walk_list(
                head,
                body + _FOURCC,
                min(end, len(head)),
                streams,
                budget,
                depth=1,
            )
            streams.complete = whole and end <= len(head)
            break
        if chunk == b"LIST" and head[body : body + _FOURCC] == b"movi":
            break
        offset = body + length + (length & 1)
    return streams


def _riff_chunk(data: bytes, offset: int) -> tuple[bytes, int]:
    """Read a RIFF chunk ID and length.

    Args:
        data: Bytes read, at least eight from ``offset``.
        offset: Offset of the chunk.

    Returns:
        The chunk ID and its data length.
    """
    length: int = struct.unpack_from("<I", data, offset + 4)[0]
    return data[offset : offset + 4], length


def _walk_list(
    data: bytes,
    start: int,
    end: int,
    streams: StreamList,
    budget: WalkBudget,
    *,
    depth: int,
) -> bool:
    """Visit the chunks of ``hdrl`` and its ``LIST strl`` children.

    Args:
        data: Bytes read.
        start: Offset of the first chunk.
        end: Offset just past the last chunk.
        streams: Streams found so far.
        budget: Shared element count.
        depth: Nesting depth of these chunks.

    Returns:
        ``False`` when a chunk is cut or overflows its list, a ``strh`` is
        shorter than its type, or a cap stopped the walk.
    """
    if depth > MAX_DEPTH:
        return False
    offset = start
    while offset < end:
        if offset + _CHUNK_HEADER > end or not budget.visit():
            return False
        chunk, length = _riff_chunk(data, offset)
        body = offset + _CHUNK_HEADER
        if body + length > end:
            return False
        if chunk == b"LIST" and data[body : body + _FOURCC] == b"strl":
            if length < _FOURCC or not _walk_list(
                data,
                body + _FOURCC,
                body + length,
                streams,
                budget,
                depth=depth + 1,
            ):
                return False
        elif chunk == b"strh":
            if length < _FOURCC:
                return False
            streams.kinds.append(_AVI_STREAM_TYPES.get(data[body : body + _FOURCC]))
        offset = body + length + (length & 1)
    return True


# --- ASF (Advanced Systems Format Specification, revision 01.20.05) ----------


def _guid(text: str) -> bytes:
    """Encode an ASF GUID as stored in the file (first three fields LE).

    Args:
        text: The GUID in its usual text form.

    Returns:
        Its 16 bytes.
    """
    return uuid.UUID(text).bytes_le


ASF_HEADER: Final[bytes] = _guid("75b22630-668e-11cf-a6d9-00aa0062ce6c")
"""ASF Header Object GUID (ASF specification, 3.1, 10.1)."""

_ASF_STREAM_PROPERTIES: Final[bytes] = _guid("b7dc0791-a9b7-11cf-8ee6-00c00c205365")
_ASF_HEADER_EXTENSION: Final[bytes] = _guid("5fbf03b5-a92e-11cf-8ee3-00c00c205365")
_ASF_EXTENDED_STREAM: Final[bytes] = _guid("14e6a5cb-c672-4332-8399-a96952065b5a")
_ASF_STREAM_TYPES: Final[dict[bytes, str]] = {
    _guid("f8699e40-5b4d-11cf-a8fd-00805f5c442b"): AUDIO,
    _guid("bc19efc0-5b4d-11cf-a8fd-00805f5c442b"): VIDEO,
    _guid("59dacfc0-59e6-11d0-a3ac-00a0c90348f6"): DATA,  # command
    _guid("b61be100-5b4e-11cf-a8fd-00805f5c4465"): DATA,  # JFIF
    _guid("35907de0-e415-11cf-a917-00805f5c442b"): DATA,  # degradable JPEG
    _guid("91bd222c-f21c-497a-8b6d-5aa86bfc0185"): DATA,  # file transfer
    _guid("3afb65e2-47ef-40f2-ac2c-70a90d71d343"): DATA,  # binary
}
"""Stream type GUIDs (ASF specification, 10.4)."""

_ASF_HEADER_BYTES: Final[int] = 30
_ASF_OBJECT_HEADER: Final[int] = 24
_ASF_EXTENSION_DATA: Final[int] = 46
_ASF_PROPERTIES_NUMBER: Final[int] = 72
_ASF_EXTENDED_FIXED: Final[int] = 88


@dataclass(slots=True)
class _AsfStreams:
    """Stream numbers the ASF header types and declares.

    Args:
        typed: Stream number to kind, from Stream Properties Objects.
        declared: Stream numbers Extended Stream Properties declare.
        complete: Whether every object was read once and whole.
        budget: Shared element count.
    """

    typed: dict[int, str | None] = field(default_factory=dict)
    declared: set[int] = field(default_factory=set)
    complete: bool = True
    budget: WalkBudget = field(default_factory=WalkBudget)


def asf_streams(content: Content) -> StreamList:
    """Read the stream types of an ASF Header Object.

    Stream Properties Objects (3.3) type each stream, at the top level or
    inside an Extended Stream Properties Object (4.1) in the Header
    Extension Object (3.4). The list is complete when the whole Header
    Object lies in the bytes read, every object was whole, no stream number
    was typed twice, and every stream an Extended Stream Properties Object
    declares has a type. A stream typed twice keeps video if either says
    video.

    Args:
        content: File content, the Header Object first.

    Returns:
        The streams in stream-number order.
    """
    head = content.head
    if len(head) < _ASF_HEADER_BYTES:
        return StreamList()
    header_size: int = struct.unpack_from("<Q", head, 16)[0]
    found = _AsfStreams()
    _walk_asf(head, _ASF_HEADER_BYTES, min(header_size, len(head)), found, depth=1)
    complete = (
        found.complete
        and header_size <= len(head)
        and found.declared <= set(found.typed)
    )
    kinds = [found.typed[number] for number in sorted(found.typed)]
    return StreamList(kinds, complete)


def _walk_asf(
    data: bytes,
    start: int,
    end: int,
    found: _AsfStreams,
    *,
    depth: int,
) -> None:
    """Visit ASF objects between ``start`` and ``end``.

    Args:
        data: Bytes read.
        start: Offset of the first object.
        end: Offset just past the last object read.
        found: Streams found so far.
        depth: Nesting depth of these objects.
    """
    if depth > MAX_DEPTH:
        found.complete = False
        return
    offset = start
    while offset + _ASF_OBJECT_HEADER <= end:
        guid = data[offset : offset + 16]
        size: int = struct.unpack_from("<Q", data, offset + 16)[0]
        if not found.budget.visit() or size < _ASF_OBJECT_HEADER or offset + size > end:
            found.complete = False
            return
        if guid == _ASF_STREAM_PROPERTIES:
            _read_asf_properties(data, offset, size, found)
        elif guid == _ASF_HEADER_EXTENSION:
            _walk_asf(
                data,
                offset + _ASF_EXTENSION_DATA,
                offset + size,
                found,
                depth=depth + 1,
            )
        elif guid == _ASF_EXTENDED_STREAM:
            _read_asf_extended(data, offset, size, found, depth=depth)
        offset += size
    if offset != end:
        found.complete = False


def _read_asf_properties(
    data: bytes,
    offset: int,
    size: int,
    found: _AsfStreams,
) -> None:
    """Record a Stream Properties Object's stream number and type (3.3).

    Args:
        data: Bytes read.
        offset: Offset of the object.
        size: Object size.
        found: Streams found so far.
    """
    if size < _ASF_PROPERTIES_NUMBER + 2:
        found.complete = False
        return
    flags: int = struct.unpack_from("<H", data, offset + _ASF_PROPERTIES_NUMBER)[0]
    number = flags & 0x7F
    kind = _ASF_STREAM_TYPES.get(data[offset + 24 : offset + 40])
    if number in found.typed:
        found.complete = False
        if found.typed[number] == VIDEO:
            return
    found.typed[number] = kind


def _read_asf_extended(
    data: bytes,
    offset: int,
    size: int,
    found: _AsfStreams,
    *,
    depth: int,
) -> None:
    """Record an Extended Stream Properties Object's stream (4.1).

    Its stream names and payload extension systems are skipped; any bytes
    left are an embedded Stream Properties Object.

    Args:
        data: Bytes read.
        offset: Offset of the object.
        size: Object size.
        found: Streams found so far.
        depth: Nesting depth of this object.
    """
    end = offset + size
    if size < _ASF_EXTENDED_FIXED:
        found.complete = False
        return
    number: int = struct.unpack_from("<H", data, offset + 72)[0]
    found.declared.add(number & 0x7F)
    counts: tuple[int, int] = struct.unpack_from("<HH", data, offset + 84)
    names, systems = counts
    position = offset + _ASF_EXTENDED_FIXED
    for _ in range(names):
        if position + 4 > end:
            found.complete = False
            return
        name_length: int = struct.unpack_from("<H", data, position + 2)[0]
        position += 4 + name_length
    for _ in range(systems):
        if position + 22 > end:
            found.complete = False
            return
        info_length: int = struct.unpack_from("<I", data, position + 18)[0]
        position += 22 + info_length
    if position + _ASF_OBJECT_HEADER <= end:
        _walk_asf(data, position, end, found, depth=depth + 1)


# --- RealMedia (RealNetworks RealMedia File Format, Helix DNA SDK) -----------


def realmedia_streams(content: Content) -> StreamList:
    """Read the MIME type of each ``MDPR`` (media properties) chunk.

    Every header chunk comes before ``DATA``, so the list is complete once
    the walk reaches ``DATA``.

    Args:
        content: File content, ``.RMF`` first.

    Returns:
        The streams in file order; logical streams are data.
    """
    head = content.head
    streams = StreamList()
    budget = WalkBudget()
    offset = 0
    while offset + 10 <= len(head) and budget.visit():
        chunk = head[offset : offset + 4]
        size: int = struct.unpack_from(">I", head, offset + 4)[0]
        if chunk == b"DATA":
            streams.complete = True
            break
        if size < 10:
            break
        if chunk == b"MDPR":
            streams.kinds.append(_realmedia_kind(head[offset + 10 : offset + size]))
        offset += size
    return streams


def _realmedia_kind(body: bytes) -> str | None:
    """Classify one ``MDPR`` chunk by its MIME type.

    Args:
        body: Chunk payload after the object version.

    Returns:
        The stream kind, or ``None`` for an unrecognised MIME type.
    """
    # stream_number, max/avg bit rate, max/avg packet size, start, preroll,
    # duration: 2 + 7 * 4 bytes, then the stream name and MIME type.
    index = 30
    if index >= len(body):
        return None
    index += 1 + body[index]
    if index >= len(body):
        return None
    mime = body[index + 1 : index + 1 + body[index]].decode("ascii", errors="replace")
    if mime.startswith("video/"):
        return VIDEO
    if mime.startswith("audio/"):
        return AUDIO
    if mime.startswith("logical-"):
        return DATA
    return None


__all__ = [
    "ASF_HEADER",
    "AUDIO",
    "DATA",
    "MAX_CHUNKS",
    "MAX_DEPTH",
    "VIDEO",
    "StreamKinds",
    "StreamList",
    "WalkBudget",
    "asf_streams",
    "avi_streams",
    "media_type_of",
    "realmedia_streams",
]
