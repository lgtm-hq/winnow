"""Stream-type walks of multi-stream containers, for rule 4 (#393).

A container's magic says what it is, not whether it holds video. For each
container here, a bounded walk over the opening bytes reads the stream
declarations the format puts before its media data, and the media type
follows from them: any video stream gives video; audio streams with no
video give audio; a stream the walk does not recognise, or no declaration
in the bytes read, gives unknown. Each walk cites its specification.
"""

from __future__ import annotations

import struct
import uuid
from collections.abc import Iterable
from typing import Final

from winnow.models.media import MediaType

VIDEO: Final[str] = "video"
AUDIO: Final[str] = "audio"
DATA: Final[str] = "data"

MAX_CHUNKS: Final[int] = 1024
"""Chunks, objects, tags or packets a walk may visit."""

StreamKinds = list[str | None]
"""Kinds of the streams a walk found; ``None`` for an unrecognised one."""


def media_type_of(kinds: Iterable[str | None]) -> MediaType | None:
    """Decide a container's media type from its streams.

    Args:
        kinds: Each stream's kind: ``"video"``, ``"audio"``, ``"data"`` or
            ``None`` for one the walk did not recognise.

    Returns:
        Video when any stream is video; audio when at least one is audio and
        every stream is recognised; otherwise ``None``.
    """
    listed = list(kinds)
    if VIDEO in listed:
        return MediaType.VIDEO
    if AUDIO in listed and None not in listed:
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


def avi_streams(head: bytes) -> StreamKinds:
    """Read the ``strh`` stream types in an AVI's ``hdrl`` list.

    Args:
        head: The file's opening bytes, ``RIFF....AVI `` first.

    Returns:
        Stream kinds in file order; empty when no ``strh`` was read.
    """
    kinds: StreamKinds = []
    _walk_riff(head, 12, len(head), kinds, depth=0)
    return kinds


def _walk_riff(
    data: bytes, start: int, end: int, kinds: StreamKinds, *, depth: int
) -> None:
    """Visit RIFF chunks, descending into ``LIST hdrl`` and ``LIST strl``.

    Args:
        data: Bytes read.
        start: Offset of the first chunk.
        end: Offset just past the last chunk.
        kinds: Stream kinds found so far.
        depth: Nesting depth.
    """
    offset = start
    visited = 0
    while offset + 8 <= end and visited < MAX_CHUNKS and depth < 4:
        visited += 1
        chunk = data[offset : offset + 4]
        (length,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        body = offset + 8
        if chunk == b"LIST" and data[body : body + 4] in {b"hdrl", b"strl"}:
            _walk_riff(data, body + 4, min(body + length, end), kinds, depth=depth + 1)
        elif chunk == b"strh" and body + 4 <= end:
            kinds.append(_AVI_STREAM_TYPES.get(data[body : body + 4]))
        elif chunk == b"LIST" and data[body : body + 4] == b"movi":
            return
        offset = body + length + (length & 1)


# --- ASF (Advanced Systems Format Specification, revision 01.20.05) ----------

ASF_HEADER: Final[bytes] = uuid.UUID("75b22630-668e-11cf-a6d9-00aa0062ce6c").bytes_le
"""ASF Header Object GUID (ASF specification, 3.1, 10.1)."""

_ASF_STREAM_PROPERTIES: Final[bytes] = uuid.UUID(
    "b7dc0791-a9b7-11cf-8ee6-00c00c205365",
).bytes_le
_ASF_STREAM_TYPES: Final[dict[bytes, str]] = {
    uuid.UUID("f8699e40-5b4d-11cf-a8fd-00805f5c442b").bytes_le: AUDIO,
    uuid.UUID("bc19efc0-5b4d-11cf-a8fd-00805f5c442b").bytes_le: VIDEO,
    uuid.UUID("59dacfc0-59e6-11d0-a3ac-00a0c90348f6").bytes_le: DATA,  # command
    uuid.UUID("b61be100-5b4e-11cf-a8fd-00805f5c4465").bytes_le: DATA,  # JFIF
    uuid.UUID("35907de0-e415-11cf-a917-00805f5c442b").bytes_le: DATA,  # JPEG
    uuid.UUID("91bd222c-f21c-497a-8b6d-5aa86bfc0185").bytes_le: DATA,  # file
    uuid.UUID("3afb65e2-47ef-40f2-ac2c-70a90d71d343").bytes_le: DATA,  # binary
}
"""Stream type GUIDs (ASF specification, 10.4)."""

_ASF_HEADER_BYTES: Final[int] = 30
_ASF_OBJECT_HEADER: Final[int] = 24


def asf_streams(head: bytes) -> StreamKinds:
    """Read the stream types of an ASF Header Object's Stream Properties.

    Args:
        head: The file's opening bytes, the Header Object first.

    Returns:
        Stream kinds in file order; empty when none was read.
    """
    kinds: StreamKinds = []
    if len(head) < _ASF_HEADER_BYTES:
        return kinds
    (header_size,) = struct.unpack("<Q", head[16:24])
    end = min(header_size, len(head))
    offset = _ASF_HEADER_BYTES
    visited = 0
    while offset + _ASF_OBJECT_HEADER <= end and visited < MAX_CHUNKS:
        visited += 1
        guid = head[offset : offset + 16]
        (size,) = struct.unpack("<Q", head[offset + 16 : offset + 24])
        if size < _ASF_OBJECT_HEADER:
            break
        if guid == _ASF_STREAM_PROPERTIES and offset + 40 <= end:
            kinds.append(_ASF_STREAM_TYPES.get(head[offset + 24 : offset + 40]))
        offset += size
    return kinds


# --- FLV (Adobe Flash Video File Format Specification 10.1, annex E) --------

_FLV_AUDIO_TAG: Final[int] = 8
_FLV_VIDEO_TAG: Final[int] = 9
_FLV_TAG_HEADER: Final[int] = 11


def flv_streams(head: bytes) -> StreamKinds:
    """Read FLV's header flags and the types of the tags in ``head``.

    Args:
        head: The file's opening bytes, ``FLV`` first.

    Returns:
        ``"video"`` when the header flags or a tag show video, ``"audio"``
        when they show audio; empty when neither.
    """
    flags = head[4]
    has_video = bool(flags & 0x01)
    has_audio = bool(flags & 0x04)
    (offset,) = struct.unpack(">I", head[5:9])
    offset += 4  # PreviousTagSize0
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
    return kinds


# --- RealMedia (RealNetworks RealMedia File Format, Helix DNA SDK) -----------


def realmedia_streams(head: bytes) -> StreamKinds:
    """Read the MIME type of each ``MDPR`` (media properties) chunk.

    Args:
        head: The file's opening bytes, ``.RMF`` first.

    Returns:
        Stream kinds in file order; logical streams are data.
    """
    kinds: StreamKinds = []
    offset = 0
    visited = 0
    while offset + 10 <= len(head) and visited < MAX_CHUNKS:
        visited += 1
        chunk = head[offset : offset + 4]
        (size,) = struct.unpack(">I", head[offset + 4 : offset + 8])
        if chunk == b"DATA" or size < 10:
            break
        if chunk == b"MDPR":
            kinds.append(_realmedia_kind(head[offset + 10 : offset + size]))
        offset += size
    return kinds


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
_PROGRAM_END: Final[int] = 0xB9
_PRIVATE_STREAM_1: Final[int] = 0xBD
_DATA_STREAM_IDS: Final[frozenset[int]] = frozenset({0xBB, 0xBC, 0xBE, 0xBF})


def program_stream_streams(head: bytes) -> StreamKinds:
    """Read the stream IDs of the PES packets in an MPEG program stream.

    ``E0``-``EF`` are video and ``C0``-``DF`` audio (13818-1, table 2-22).
    In ``private_stream_1`` the first payload byte names a DVD substream:
    ``80``-``8F`` AC-3 or DTS and ``A0``-``AF`` LPCM are audio, ``20``-``3F``
    subpictures are data (DVD-Video, part 3).

    Args:
        head: The file's opening bytes, a pack header first.

    Returns:
        Stream kinds in file order, up to the first malformed packet; empty
        when a pack header is malformed.
    """
    kinds: StreamKinds = []
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
                return []
            offset += length
            continue
        (length,) = struct.unpack(">H", head[offset + 4 : offset + 6])
        kinds.append(_pes_kind(head, offset, stream_id))
        offset += 6 + length
    return kinds


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


def ogg_streams(head: bytes) -> StreamKinds:
    """Read the first packet of every beginning-of-stream page.

    RFC 3533, section 4: every logical stream's BOS page comes before any
    other page, so the BOS pages at the start list every stream.

    Args:
        head: The file's opening bytes, ``OggS`` first.

    Returns:
        Stream kinds in file order; empty when the first page is not a
        valid BOS page.
    """
    kinds: StreamKinds = []
    offset = 0
    while offset + _OGG_PAGE_HEADER <= len(head) and len(kinds) < MAX_CHUNKS:
        page = head[offset : offset + _OGG_PAGE_HEADER]
        if page[:4] != b"OggS" or page[4] != 0 or not page[5] & _OGG_BOS:
            break
        segments = page[26]
        table = head[offset + _OGG_PAGE_HEADER : offset + _OGG_PAGE_HEADER + segments]
        body = offset + _OGG_PAGE_HEADER + segments
        if len(table) < segments:
            return []
        packet = 0
        for lacing in table:
            packet += lacing
            if lacing < 255:
                break
        first = head[body : body + packet]
        kinds.append(
            next(
                (kind for prefix, kind in OGG_CODECS if first.startswith(prefix)), None
            )
        )
        offset = body + sum(table)
    return kinds


__all__ = [
    "ASF_HEADER",
    "AUDIO",
    "DATA",
    "MAX_CHUNKS",
    "OGG_CODECS",
    "VIDEO",
    "StreamKinds",
    "asf_streams",
    "avi_streams",
    "flv_streams",
    "media_type_of",
    "mpeg_video_sequence",
    "ogg_streams",
    "program_stream_streams",
    "realmedia_streams",
]
