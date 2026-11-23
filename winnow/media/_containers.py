"""Stream-type walks of multi-stream containers, for rule 4 (#393).

A container's magic says what it is, not whether it holds video. For each
container here, a bounded walk over the opening bytes reads the stream
declarations the format puts before its media data. Each walk cites its
specification and reports whether it read the whole declaration list:

* any video stream gives video, even from a partial list;
* audio is decided only from a complete list in which every stream is
  recognised and at least one is audio;
* anything else, including a walk cut short by the bytes read or by
  :data:`MAX_CHUNKS`, gives no answer, and the caller asks ffprobe.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from typing import Final

from winnow.models.media import MediaType

VIDEO: Final[str] = "video"
AUDIO: Final[str] = "audio"
DATA: Final[str] = "data"

MAX_CHUNKS: Final[int] = 1024
"""Chunks, objects, tags or packets a walk may visit."""

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


def avi_streams(head: bytes) -> StreamList:
    """Read the ``strh`` stream types in an AVI's ``hdrl`` list.

    The list is complete when the whole ``LIST hdrl`` lies in ``head``.

    Args:
        head: The file's opening bytes, ``RIFF....AVI `` first.

    Returns:
        The streams in file order.
    """
    streams = StreamList()
    offset = 12
    visited = 0
    while offset + 12 <= len(head) and visited < MAX_CHUNKS:
        visited += 1
        chunk, length = _riff_chunk(head, offset)
        body = offset + 8
        if chunk == b"LIST" and head[body : body + 4] == b"hdrl":
            end = body + length
            budget = _walk_hdrl(head, body + 4, min(end, len(head)), streams)
            streams.complete = end <= len(head) and budget
            return streams
        if chunk == b"LIST" and head[body : body + 4] == b"movi":
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


def _walk_hdrl(data: bytes, start: int, end: int, streams: StreamList) -> bool:
    """Visit ``hdrl`` and its ``LIST strl`` children for ``strh`` chunks.

    Args:
        data: Bytes read.
        start: Offset of the first chunk.
        end: Offset just past the last chunk read.
        streams: Streams found so far.

    Returns:
        ``False`` when :data:`MAX_CHUNKS` stopped the walk.
    """
    offset = start
    visited = 0
    while offset + 8 <= end:
        visited += 1
        if visited > MAX_CHUNKS:
            return False
        chunk, length = _riff_chunk(data, offset)
        body = offset + 8
        if chunk == b"LIST" and data[body : body + 4] == b"strl":
            if not _walk_hdrl(data, body + 4, min(body + length, end), streams):
                return False
        elif chunk == b"strh" and body + 4 <= end:
            streams.kinds.append(_AVI_STREAM_TYPES.get(data[body : body + 4]))
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
        complete: Whether every object was read.
    """

    typed: dict[int, str | None] = field(default_factory=dict)
    declared: set[int] = field(default_factory=set)
    complete: bool = True


def asf_streams(head: bytes) -> StreamList:
    """Read the stream types of an ASF Header Object.

    Stream Properties Objects (3.3) type each stream, at the top level or
    inside an Extended Stream Properties Object (4.1) in the Header
    Extension Object (3.4). The list is complete when the whole Header
    Object lies in ``head`` and every stream an Extended Stream Properties
    Object declares has a type.

    Args:
        head: The file's opening bytes, the Header Object first.

    Returns:
        The streams in stream-number order.
    """
    if len(head) < _ASF_HEADER_BYTES:
        return StreamList()
    header_size: int = struct.unpack_from("<Q", head, 16)[0]
    found = _AsfStreams()
    _walk_asf(head, _ASF_HEADER_BYTES, min(header_size, len(head)), found)
    complete = (
        found.complete
        and header_size <= len(head)
        and found.declared <= set(found.typed)
    )
    return StreamList([found.typed[number] for number in sorted(found.typed)], complete)


def _walk_asf(data: bytes, start: int, end: int, found: _AsfStreams) -> None:
    """Visit ASF objects between ``start`` and ``end``.

    Args:
        data: Bytes read.
        start: Offset of the first object.
        end: Offset just past the last object read.
        found: Streams found so far.
    """
    offset = start
    visited = 0
    while offset + _ASF_OBJECT_HEADER <= end:
        visited += 1
        guid = data[offset : offset + 16]
        size: int = struct.unpack_from("<Q", data, offset + 16)[0]
        if visited > MAX_CHUNKS or size < _ASF_OBJECT_HEADER or offset + size > end:
            found.complete = False
            return
        if guid == _ASF_STREAM_PROPERTIES:
            _read_asf_properties(data, offset, size, found)
        elif guid == _ASF_HEADER_EXTENSION:
            _walk_asf(data, offset + _ASF_EXTENSION_DATA, offset + size, found)
        elif guid == _ASF_EXTENDED_STREAM:
            _read_asf_extended(data, offset, size, found)
        offset += size


def _read_asf_properties(
    data: bytes, offset: int, size: int, found: _AsfStreams
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
    number = struct.unpack_from("<H", data, offset + _ASF_PROPERTIES_NUMBER)[0] & 0x7F
    found.typed[number] = _ASF_STREAM_TYPES.get(data[offset + 24 : offset + 40])


def _read_asf_extended(data: bytes, offset: int, size: int, found: _AsfStreams) -> None:
    """Record an Extended Stream Properties Object's stream (4.1).

    Its stream names and payload extension systems are skipped; any bytes
    left are an embedded Stream Properties Object.

    Args:
        data: Bytes read.
        offset: Offset of the object.
        size: Object size.
        found: Streams found so far.
    """
    end = offset + size
    if size < _ASF_EXTENDED_FIXED:
        found.complete = False
        return
    found.declared.add(struct.unpack_from("<H", data, offset + 72)[0] & 0x7F)
    names, systems = struct.unpack_from("<HH", data, offset + 84)
    position = offset + _ASF_EXTENDED_FIXED
    for _ in range(names):
        if position + 4 > end:
            found.complete = False
            return
        position += 4 + struct.unpack_from("<H", data, position + 2)[0]
    for _ in range(systems):
        if position + 22 > end:
            found.complete = False
            return
        position += 22 + struct.unpack_from("<I", data, position + 18)[0]
    if position + _ASF_OBJECT_HEADER <= end:
        _walk_asf(data, position, end, found)


# --- FLV (Adobe Flash Video File Format Specification 10.1, annex E) --------

_FLV_AUDIO_TAG: Final[int] = 8
_FLV_VIDEO_TAG: Final[int] = 9
_FLV_TAG_HEADER: Final[int] = 11


def flv_streams(head: bytes) -> StreamList:
    """Read FLV's header flags and the types of the tags in ``head``.

    The header's ``TypeFlagsAudio`` and ``TypeFlagsVideo`` (E.2) declare
    which tag types the file holds, so the list is complete; a video tag
    seen in ``head`` adds video even if the flag is clear.

    Args:
        head: The file's opening bytes, ``FLV`` first.

    Returns:
        ``"video"`` and ``"audio"`` as declared or seen.
    """
    flags = head[4]
    has_video = bool(flags & 0x01)
    has_audio = bool(flags & 0x04)
    offset: int = struct.unpack_from(">I", head, 5)[0] + 4  # PreviousTagSize0
    visited = 0
    while offset + _FLV_TAG_HEADER <= len(head) and visited < MAX_CHUNKS:
        visited += 1
        tag_type = head[offset] & 0x1F
        size = int.from_bytes(head[offset + 1 : offset + 4], "big")
        has_video = has_video or tag_type == _FLV_VIDEO_TAG
        has_audio = has_audio or tag_type == _FLV_AUDIO_TAG
        offset += _FLV_TAG_HEADER + size + 4
    kinds: StreamKinds = []
    if has_video:
        kinds.append(VIDEO)
    if has_audio:
        kinds.append(AUDIO)
    return StreamList(kinds, complete=True)


# --- RealMedia (RealNetworks RealMedia File Format, Helix DNA SDK) -----------


def realmedia_streams(head: bytes) -> StreamList:
    """Read the MIME type of each ``MDPR`` (media properties) chunk.

    Every header chunk comes before ``DATA``, so the list is complete once
    the walk reaches ``DATA``.

    Args:
        head: The file's opening bytes, ``.RMF`` first.

    Returns:
        The streams in file order; logical streams are data.
    """
    streams = StreamList()
    offset = 0
    visited = 0
    while offset + 10 <= len(head) and visited < MAX_CHUNKS:
        visited += 1
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


# --- MPEG program stream (ISO/IEC 13818-1, 2.5; ISO/IEC 11172-1) -------------

_PACK_START: Final[int] = 0xBA
_SYSTEM_HEADER: Final[int] = 0xBB
_PROGRAM_END: Final[int] = 0xB9
_PRIVATE_STREAM_1: Final[int] = 0xBD
_DATA_STREAM_IDS: Final[frozenset[int]] = frozenset({0xBB, 0xBC, 0xBE, 0xBF})


def program_stream_streams(head: bytes) -> StreamList:
    """Read the stream IDs of the PES packets in an MPEG program stream.

    ``E0``-``EF`` are video and ``C0``-``DF`` audio (13818-1, table 2-22).
    In ``private_stream_1`` the first payload byte names a DVD substream:
    ``80``-``8F`` AC-3 or DTS and ``A0``-``AF`` LPCM are audio, ``20``-``3F``
    subpictures are data (DVD-Video, part 3).

    A program stream has no stream list: packets interleave. The list counts
    as complete only when a system header (2.5.3.5) gives ``video_bound``
    0, so no video stream can follow; audio is then decided from the
    packets read.

    Args:
        head: The file's opening bytes, a pack header first.

    Returns:
        The stream kinds of the packets read, up to the first malformed
        packet; empty when a pack header is malformed.
    """
    streams = StreamList()
    offset = 0
    visited = 0
    while offset + 6 <= len(head) and visited < MAX_CHUNKS:
        visited += 1
        if head[offset : offset + 3] != b"\x00\x00\x01":
            break
        stream_id = head[offset + 3]
        if stream_id == _PROGRAM_END:
            break
        if stream_id == _PACK_START:
            length = _pack_header_length(head, offset)
            if length is None:
                return StreamList()
            offset += length
            continue
        if stream_id == _SYSTEM_HEADER and offset + 11 <= len(head):
            streams.complete = head[offset + 10] & 0x1F == 0
        packet_length: int = struct.unpack_from(">H", head, offset + 4)[0]
        streams.kinds.append(_pes_kind(head, offset, stream_id))
        offset += 6 + packet_length
    return streams


def _pack_header_length(head: bytes, offset: int) -> int | None:
    """Return the length of the pack header at ``offset``.

    Args:
        head: Bytes read.
        offset: Offset of the pack start code.

    Returns:
        14 plus stuffing for MPEG-2, 12 for MPEG-1, ``None`` otherwise.
    """
    if offset + 14 > len(head):
        return None
    marker = head[offset + 4]
    if marker >> 6 == 0b01:
        return 14 + (head[offset + 13] & 0x7)
    if marker >> 4 == 0b0010:
        return 12
    return None


def _pes_kind(head: bytes, offset: int, stream_id: int) -> str | None:
    """Classify one PES packet by its stream ID.

    Args:
        head: Bytes read.
        offset: Offset of the packet start code.
        stream_id: The packet's stream ID.

    Returns:
        The stream kind, or ``None`` when unrecognised.
    """
    if 0xE0 <= stream_id <= 0xEF:
        return VIDEO
    if 0xC0 <= stream_id <= 0xDF:
        return AUDIO
    if stream_id in _DATA_STREAM_IDS:
        return DATA
    if stream_id == _PRIVATE_STREAM_1:
        return _dvd_substream_kind(head, offset)
    return None


def _dvd_substream_kind(head: bytes, offset: int) -> str | None:
    """Classify a ``private_stream_1`` packet by its DVD substream ID.

    Args:
        head: Bytes read.
        offset: Offset of the packet start code.

    Returns:
        The stream kind, or ``None`` when unrecognised.
    """
    if offset + 9 > len(head):
        return None
    payload = offset + 9 + head[offset + 8]
    if payload >= len(head):
        return None
    substream = head[payload]
    if 0x80 <= substream <= 0x8F or 0xA0 <= substream <= 0xAF:
        return AUDIO
    if 0x20 <= substream <= 0x3F:
        return DATA
    return None


def mpeg_video_sequence(head: bytes) -> bool:
    """Check an MPEG-1/2 video sequence header (ISO/IEC 13818-2, 6.2.2.1).

    Args:
        head: The file's opening bytes.

    Returns:
        ``True`` for ``00 00 01 B3`` with a non-zero picture size, a valid
        aspect ratio and a valid frame rate code.
    """
    if len(head) < 12 or head[:4] != b"\x00\x00\x01\xb3":
        return False
    width = (head[4] << 4) | (head[5] >> 4)
    height = ((head[5] & 0x0F) << 8) | head[6]
    aspect = head[7] >> 4
    rate = head[7] & 0x0F
    return width > 0 and height > 0 and 1 <= aspect <= 14 and 1 <= rate <= 8


# --- Ogg (RFC 3533) ---------------------------------------------------------

OGG_CODECS: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\x01vorbis", AUDIO),  # Vorbis I specification, 4.2.1
    (b"OpusHead", AUDIO),  # RFC 7845, 5.1
    (b"\x7fFLAC", AUDIO),  # FLAC-to-Ogg mapping, RFC 9639 section 10.1
    (b"Speex   ", AUDIO),  # Speex manual, Ogg mapping
    (b"\x80theora", VIDEO),  # Theora specification, 6.2
    (b"OVP80", VIDEO),  # VP8-in-Ogg mapping (Xiph wiki OggVP8)
    (b"BBCD\x00", VIDEO),  # Dirac-in-Ogg mapping
    (b"fishead\x00", DATA),  # Ogg Skeleton 3/4: an index, not media
)
"""First-packet prefixes of the Ogg codec mappings."""

_OGG_PAGE_HEADER: Final[int] = 27
_OGG_BOS: Final[int] = 0x02


def ogg_streams(head: bytes) -> StreamList:
    """Read the first packet of every beginning-of-stream page.

    RFC 3533, section 4: every logical stream's BOS page comes before any
    other page, so the list is complete once a page that is not a BOS page
    is reached.

    Args:
        head: The file's opening bytes, ``OggS`` first.

    Returns:
        The streams in file order; empty when the first page is not a
        valid BOS page.
    """
    streams = StreamList()
    offset = 0
    while offset + _OGG_PAGE_HEADER <= len(head) and len(streams.kinds) < MAX_CHUNKS:
        page = head[offset : offset + _OGG_PAGE_HEADER]
        if page[:4] != b"OggS" or page[4] != 0:
            break
        if not page[5] & _OGG_BOS:
            streams.complete = bool(streams.kinds)
            break
        segments = page[26]
        table = head[offset + _OGG_PAGE_HEADER : offset + _OGG_PAGE_HEADER + segments]
        body = offset + _OGG_PAGE_HEADER + segments
        if len(table) < segments:
            return StreamList()
        packet = 0
        for lacing in table:
            packet += lacing
            if lacing < 255:
                break
        first = head[body : body + packet]
        streams.kinds.append(
            next(
                (kind for prefix, kind in OGG_CODECS if first.startswith(prefix)), None
            ),
        )
        offset = body + sum(table)
    return streams


__all__ = [
    "ASF_HEADER",
    "AUDIO",
    "DATA",
    "MAX_CHUNKS",
    "OGG_CODECS",
    "VIDEO",
    "StreamKinds",
    "StreamList",
    "asf_streams",
    "avi_streams",
    "flv_streams",
    "media_type_of",
    "mpeg_video_sequence",
    "ogg_streams",
    "program_stream_streams",
    "realmedia_streams",
]
