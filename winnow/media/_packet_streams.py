"""Stream-type walks of tag, packet and page streams, for rule 4 (#393).

FLV, MPEG program streams and Ogg have no single stream table up front:
their streams show up in tags, packets and pages. The rules of
:mod:`winnow.media._containers` apply: any video stream gives video; audio
needs a complete list in which every stream is recognised. What counts as
complete differs per format and is stated on each walk.
"""

from __future__ import annotations

import os
import struct
from typing import Final

from winnow.media._containers import (
    AUDIO,
    DATA,
    VIDEO,
    StreamList,
    WalkBudget,
)
from winnow.media._detect_base import BudgetExceededError, Content

# --- FLV (Adobe Flash Video File Format Specification 10.1, annex E) --------

_FLV_AUDIO_TAG: Final[int] = 8
_FLV_VIDEO_TAG: Final[int] = 9
_FLV_SCRIPT_TAG: Final[int] = 18
_FLV_TAG_HEADER: Final[int] = 11
_FLV_PREVIOUS_SIZE: Final[int] = 4

MAX_FLV_TAGS: Final[int] = 23_000
"""FLV tag headers the walk may read before its list counts as incomplete."""


def flv_streams(content: Content) -> StreamList:
    """Read FLV's header flags and the types of its tags (E.2, E.4.1).

    ``TypeFlagsVideo`` or any video tag gives video. The header flags are
    not trusted for audio: the list is complete only when the walk visits
    every tag and ends exactly at the end of the file. Only the 11-byte tag
    headers are read, seeking past each payload, up to :data:`MAX_FLV_TAGS`
    tags: about nine minutes of audio-only FLV (AAC or MP3 at about 43
    tags a second) or five minutes with 30 fps video.

    Args:
        content: File content, ``FLV`` first.

    Returns:
        ``"video"`` when seen or flagged, ``"audio"`` for audio tags or the
        audio flag, ``None`` for a tag of another type.
    """
    head = content.head
    flags = head[4]
    has_video = bool(flags & 0x01)
    has_audio = bool(flags & 0x04)
    has_other = False
    tags = 0
    data_offset: int = struct.unpack_from(">I", head, 5)[0]
    offset = data_offset + _FLV_PREVIOUS_SIZE
    complete = False
    while not has_video:
        if offset == content.size:
            complete = True
            break
        tags += 1
        if offset + _FLV_TAG_HEADER > content.size or tags > MAX_FLV_TAGS:
            break
        header = _tag_header(content, offset)
        if header is None:
            break
        tag_type = header[0] & 0x1F
        has_video = tag_type == _FLV_VIDEO_TAG
        has_audio = has_audio or tag_type == _FLV_AUDIO_TAG
        has_other = has_other or tag_type not in {
            _FLV_AUDIO_TAG,
            _FLV_VIDEO_TAG,
            _FLV_SCRIPT_TAG,
        }
        size = int.from_bytes(header[1:4], "big")
        offset += _FLV_TAG_HEADER + size + _FLV_PREVIOUS_SIZE
    kinds: list[str | None] = []
    if has_video:
        kinds.append(VIDEO)
    if has_audio:
        kinds.append(AUDIO)
    if has_other:
        kinds.append(None)
    return StreamList(kinds, complete)


def _tag_header(content: Content, offset: int) -> bytes | None:
    """Return the 11-byte FLV tag header at ``offset``.

    Args:
        content: File content.
        offset: File offset of the tag.

    Returns:
        The header, or ``None`` when it cannot be read whole.
    """
    header = bytes_at(content, offset, _FLV_TAG_HEADER)
    return header if header is not None and len(header) == _FLV_TAG_HEADER else None


def bytes_at(content: Content, offset: int, length: int) -> bytes | None:
    """Return up to ``length`` bytes at ``offset``, reading only those.

    Within the opening bytes they come from the head. Past it they are read
    with a positioned read of the open descriptor (``os.pread``), which
    reads exactly these bytes and leaves the shared handle's position
    alone; where ``pread`` does not exist, the shared reader is used within
    its budget.

    Args:
        content: File content.
        offset: File offset.
        length: Bytes wanted; fewer come back at the end of the file.

    Returns:
        The bytes, or ``None`` when the read budget is spent.
    """
    head = content.head
    if offset + length <= len(head) or len(head) == content.size:
        return head[offset : offset + length]
    pread = getattr(os, "pread", None)
    try:
        if pread is not None and content.descriptor >= 0:
            data: bytes = pread(content.descriptor, length, offset)
            return data
        return content.reader.read_at(offset, length)
    except BudgetExceededError:
        return None


# --- MPEG program stream (ISO/IEC 13818-1, 2.5; ISO/IEC 11172-1) -------------

_PACK_START: Final[int] = 0xBA
_SYSTEM_HEADER: Final[int] = 0xBB
_PROGRAM_END: Final[int] = 0xB9
_PRIVATE_STREAM_1: Final[int] = 0xBD
_DATA_STREAM_IDS: Final[frozenset[int]] = frozenset({0xBB, 0xBC, 0xBE, 0xBF})
_MPEG2_PACK: Final[int] = 14
_MPEG1_PACK: Final[int] = 12
_PES_HEADER: Final[int] = 6


MAX_PROGRAM_STREAM_PACKETS: Final[int] = 46_000
"""Pack and PES headers the program stream walk may read."""

_PACKET_WINDOW: Final[int] = 24
"""Bytes read per header past the head: a pack header with stuffing, or a
PES header up to its ``PES_header_data_length``."""


def program_stream_streams(content: Content) -> StreamList:
    """Read the stream IDs of the PES packets in an MPEG program stream.

    ``E0``-``EF`` are video and ``C0``-``DF`` audio (13818-1, table 2-22).
    In ``private_stream_1`` the first payload byte names a DVD substream:
    ``80``-``8F`` AC-3 or DTS and ``A0``-``AF`` LPCM are audio, ``20``-``3F``
    subpictures are data (DVD-Video, part 3).

    A program stream has no stream list: packets interleave. The list counts
    as complete only when a system header (2.5.3.5) gives ``video_bound``
    0, so no video stream can follow, and the walk read every packet: it
    reached the ``program_end_code`` or the exact end of the file. Past the
    opening bytes only the pack and PES headers are read (24 bytes each,
    with positioned reads), seeking past each payload, up to
    :data:`MAX_PROGRAM_STREAM_PACKETS` headers. In 2,048-byte packs (a
    pack and a PES header each) that is about 47 MB: 32 minutes of
    audio-only MPEG-1 Layer II at 192 kbit/s, 16 minutes at 384. A cut,
    corrupt bytes or the cap leave the list incomplete, keeping what was
    found.

    Args:
        content: File content, a pack header first.

    Returns:
        The stream kinds of the packets read.
    """
    streams = StreamList()
    no_video = False
    whole = False
    offset = 0
    headers = 0
    while True:
        if offset == content.size:
            whole = True
            break
        headers += 1
        if headers > MAX_PROGRAM_STREAM_PACKETS:
            break
        window = bytes_at(content, offset, _PACKET_WINDOW)
        if window is None or len(window) < 4 or window[:3] != b"\x00\x00\x01":
            break
        stream_id = window[3]
        if stream_id == _PROGRAM_END:
            whole = True
            break
        length = _packet_length(content, offset, window, streams)
        if length is None:
            break
        if stream_id == _SYSTEM_HEADER and len(window) >= 11:
            no_video = window[10] & 0x1F == 0
        offset += length
    streams.complete = no_video and whole
    return streams


def _packet_length(
    content: Content, offset: int, window: bytes, streams: StreamList
) -> int | None:
    """Return the length of the pack or PES packet at ``offset``.

    A PES packet's stream kind is added to ``streams``; for
    ``private_stream_1`` the header is read up to its first payload byte.

    Args:
        content: File content.
        offset: File offset of the start code.
        window: The bytes read at ``offset``.
        streams: Stream list being built.

    Returns:
        The length, or ``None`` for a header cut by the end of the file.
    """
    stream_id = window[3]
    if stream_id == _PACK_START:
        return _pack_header_length(window, 0)
    if len(window) < _PES_HEADER:
        return None
    if stream_id == _PRIVATE_STREAM_1 and len(window) >= 9:
        window = bytes_at(content, offset, 10 + window[8]) or window
    streams.kinds.append(_pes_kind(window, 0, stream_id))
    packet_length: int = struct.unpack_from(">H", window, 4)[0]
    return _PES_HEADER + packet_length


def _pack_header_length(head: bytes, offset: int) -> int | None:
    """Return the length of the pack header at ``offset``.

    Args:
        head: Bytes read.
        offset: Offset of the pack start code.

    Returns:
        14 plus stuffing for MPEG-2, 12 for MPEG-1, ``None`` for another
        marker or a header cut by the bytes read.
    """
    if offset + 5 > len(head):
        return None
    marker = head[offset + 4]
    if marker >> 6 == 0b01 and offset + _MPEG2_PACK <= len(head):
        return _MPEG2_PACK + (head[offset + 13] & 0x7)
    if marker >> 4 == 0b0010 and offset + _MPEG1_PACK <= len(head):
        return _MPEG1_PACK
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

    Only an MPEG-2 PES header (marker bits ``10``, 13818-1 2.4.3.7) has
    the ``PES_header_data_length`` this reads.

    Args:
        head: Bytes read.
        offset: Offset of the packet start code.

    Returns:
        The stream kind, or ``None`` when unrecognised.
    """
    if offset + 9 > len(head) or head[offset + 6] >> 6 != 0b10:
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
    # OGM (DirectShow Ogg) stream headers: packet type 1 and the stream type.
    (b"\x01video\x00\x00\x00", VIDEO),
    (b"\x01audio\x00\x00\x00", AUDIO),
    (b"\x01text\x00\x00\x00\x00", DATA),
)
"""First-packet prefixes of the Ogg codec mappings."""

_OGG_PAGE_HEADER: Final[int] = 27
_OGG_BOS: Final[int] = 0x02


def ogg_streams(content: Content) -> StreamList:
    """Read the first packet of every beginning-of-stream page.

    RFC 3533, section 4: every logical stream's BOS page comes before any
    other page, so the list is complete once a page that is not a BOS page
    is reached. A BOS page cut by the bytes read ends the walk incomplete,
    keeping the streams already found.

    Args:
        content: File content, ``OggS`` first.

    Returns:
        The streams in file order.
    """
    head = content.head
    streams = StreamList()
    budget = WalkBudget()
    offset = 0
    while offset + _OGG_PAGE_HEADER <= len(head) and budget.visit():
        page = head[offset : offset + _OGG_PAGE_HEADER]
        if page[:4] != b"OggS" or page[4] != 0:
            break
        if not page[5] & _OGG_BOS:
            streams.complete = bool(streams.kinds)
            break
        segments = page[26]
        body = offset + _OGG_PAGE_HEADER + segments
        table = head[offset + _OGG_PAGE_HEADER : body]
        if len(table) < segments:
            break
        packet = 0
        for lacing in table:
            packet += lacing
            if lacing < 255:
                break
        first = head[body : body + packet]
        kind = next((k for prefix, k in OGG_CODECS if first.startswith(prefix)), None)
        if kind is None and body + packet > len(head):
            break
        streams.kinds.append(kind)
        offset = body + sum(table)
    return streams


__all__ = [
    "MAX_FLV_TAGS",
    "MAX_PROGRAM_STREAM_PACKETS",
    "OGG_CODECS",
    "bytes_at",
    "flv_streams",
    "mpeg_video_sequence",
    "ogg_streams",
    "program_stream_streams",
]
