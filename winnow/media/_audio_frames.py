"""Frame-header checks for elementary audio streams, for rule 4 (#393).

MPEG audio, ADTS AAC, AC-3, E-AC-3 and DTS have no file header: a file is
a run of frames, each opening with a sync pattern. A short sync pattern
occurs by chance in other data, so a stream is recognised only when every
field of the frame header is valid and :data:`MIN_FRAMES` frames follow
each other exactly, each starting where the previous one's length ends (or
fewer when the last one ends exactly at the end of the file).

An ID3v2 tag is not audio by itself: :func:`skip_id3v2` validates its
header and synchsafe size so the frames after it can be checked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

MIN_FRAMES: Final[int] = 3
"""Consecutive valid frames needed to recognise an elementary stream."""

MAX_ID3_TAGS: Final[int] = 4
"""ID3v2 tags skipped in a row before giving up."""

FrameLength = Callable[[bytes], int | None]
"""Return a frame's length from its header bytes, or ``None`` if invalid."""

_MPEG_HEADER: Final[int] = 4
_ADTS_HEADER: Final[int] = 7
_AC3_HEADER: Final[int] = 6
_DTS_HEADER: Final[int] = 8

# ISO/IEC 11172-3, 2.4.2.3 and ISO/IEC 13818-3, 2.4.2.3: kbit/s by
# (MPEG-1?, layer) and bitrate index; index 0 (free format) and 15 are
# not accepted.
_MPEG_BITRATES: Final[dict[tuple[bool, int], tuple[int, ...]]] = {
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MPEG1_RATES: Final[tuple[int, int, int]] = (44100, 48000, 32000)
_MPEG_RATE_DIVISORS: Final[dict[int, int]] = {0b11: 1, 0b10: 2, 0b00: 4}
_MPEG_LAYERS: Final[dict[int, int]] = {0b11: 1, 0b10: 2, 0b01: 3}

_AC3_BITRATES: Final[tuple[int, ...]] = (
    *(32, 40, 48, 56, 64, 80, 96, 112, 128, 160),
    *(192, 224, 256, 320, 384, 448, 512, 576, 640),
)
"""AC-3 kbit/s by ``frmsizecod // 2`` (ATSC A/52, table 5.18)."""

_AC3_MAX_BSID: Final[int] = 10
_EAC3_MIN_BSID: Final[int] = 11
_EAC3_MAX_BSID: Final[int] = 16

DTS_SYNC: Final[bytes] = bytes.fromhex("7ffe8001")
"""DTS core sync word, 16-bit big-endian form (ETSI TS 102 114, 5.3.1)."""

_DTS_MIN_FSIZE: Final[int] = 95


def mpeg_audio_frame_length(header: bytes) -> int | None:
    """Return an MPEG-1/2/2.5 audio frame's length from its header.

    Args:
        header: At least four bytes at the frame start.

    Returns:
        The frame length in bytes, or ``None`` when a field is invalid.
    """
    if len(header) < _MPEG_HEADER or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version = (header[1] >> 3) & 0x3
    layer = _MPEG_LAYERS.get((header[1] >> 1) & 0x3)
    divisor = _MPEG_RATE_DIVISORS.get(version)
    bitrate_index = header[2] >> 4
    rate_index = (header[2] >> 2) & 0x3
    if layer is None or divisor is None or rate_index == 0x3:
        return None
    if bitrate_index in {0, 0xF} or header[3] & 0x3 == 0b10:
        return None
    mpeg1 = version == 0b11
    bitrate = _MPEG_BITRATES[(mpeg1, layer)][bitrate_index] * 1000
    rate = _MPEG1_RATES[rate_index] // divisor
    padding = (header[2] >> 1) & 0x1
    if layer == 1:
        return (12 * bitrate // rate + padding) * 4
    if layer == 3 and not mpeg1:
        return 72 * bitrate // rate + padding
    return 144 * bitrate // rate + padding


def adts_frame_length(header: bytes) -> int | None:
    """Return an ADTS frame's length from its header (ISO/IEC 13818-7, 6.2).

    The sync word, layer and ID are checked with a mask (``FFF`` then layer
    ``00``), so frames with and without CRC and of any profile match.

    Args:
        header: At least seven bytes at the frame start.

    Returns:
        The frame length in bytes, or ``None`` when a field is invalid.
    """
    if len(header) < _ADTS_HEADER or header[0] != 0xFF or header[1] & 0xF6 != 0xF0:
        return None
    if (header[2] >> 2) & 0xF > 12:
        return None
    length = ((header[3] & 0x3) << 11) | (header[4] << 3) | (header[5] >> 5)
    minimum = _ADTS_HEADER + (0 if header[1] & 0x1 else 2)
    return length if length > minimum else None


def ac3_frame_length(header: bytes) -> int | None:
    """Return an AC-3 or E-AC-3 frame's length from its header.

    AC-3: ATSC A/52, 5.3 (``bsid`` up to 10, ``frmsizecod`` in table
    5.18). E-AC-3: A/52 annex E, E.1.2 (``bsid`` 11 to 16, ``frmsiz``).

    Args:
        header: At least six bytes at the frame start.

    Returns:
        The frame length in bytes, or ``None`` when a field is invalid.
    """
    if len(header) < _AC3_HEADER or header[:2] != b"\x0b\x77":
        return None
    bsid = header[5] >> 3
    if bsid <= _AC3_MAX_BSID:
        fscod = header[4] >> 6
        code = header[4] & 0x3F
        if fscod == 0x3 or code >= 2 * len(_AC3_BITRATES):
            return None
        kbps = _AC3_BITRATES[code // 2]
        words = (kbps * 2, kbps * 320 // 147 + (code & 1), kbps * 3)[fscod]
        return words * 2
    if _EAC3_MIN_BSID <= bsid <= _EAC3_MAX_BSID:
        if header[2] >> 6 == 0x3 or header[4] >> 4 == 0xF:
            return None
        return ((((header[2] & 0x7) << 8) | header[3]) + 1) * 2
    return None


def dts_frame_length(header: bytes) -> int | None:
    """Return a DTS core frame's length (ETSI TS 102 114, 5.3.1).

    Args:
        header: At least eight bytes at the frame start.

    Returns:
        The frame length in bytes, or ``None`` when a field is invalid.
    """
    if len(header) < _DTS_HEADER or header[:4] != DTS_SYNC:
        return None
    fsize = ((header[5] & 0x3) << 12) | (header[6] << 4) | (header[7] >> 4)
    return fsize + 1 if fsize >= _DTS_MIN_FSIZE else None


def frames_follow(
    data: bytes,
    offset: int,
    *,
    size: int,
    frame_length: FrameLength,
    data_offset: int = 0,
) -> bool:
    """Check that valid frames follow each other from ``offset``.

    Args:
        data: Bytes read from the file, starting at file offset
            ``data_offset``.
        offset: File offset of the first frame.
        size: File size in bytes.
        frame_length: Returns a frame's length from its header bytes.
        data_offset: File offset of ``data[0]``.

    Returns:
        ``True`` for :data:`MIN_FRAMES` valid frames in a row, or at least
        one valid frame per step up to the exact end of the file.
    """
    count = 0
    position = offset
    while count < MIN_FRAMES:
        if position == size and count > 0:
            return True
        start = position - data_offset
        length = frame_length(data[start : start + 16])
        if length is None:
            return False
        count += 1
        position += length
    return True


def skip_id3v2(data: bytes, offset: int) -> int | None:
    """Return the offset just past an ID3v2 tag (ID3v2.4.0 structure, 3.1).

    The header must be ``ID3``, a major version of 2 to 4, a revision below
    ``FF``, and a synchsafe size (each byte below ``80``). A footer (flag
    bit 4 in version 4) adds ten bytes.

    Args:
        data: Bytes holding the tag header at ``offset``.
        offset: Offset of the tag header in ``data``.

    Returns:
        The offset after the tag, or ``None`` when there is no valid tag.
    """
    header = data[offset : offset + 10]
    if len(header) < 10 or not header.startswith(b"ID3"):
        return None
    if header[3] not in {2, 3, 4} or header[4] == 0xFF:
        return None
    if any(byte & 0x80 for byte in header[6:10]):
        return None
    size = 0
    for byte in header[6:10]:
        size = (size << 7) | byte
    footer = 10 if header[3] == 4 and header[5] & 0x10 else 0
    return offset + 10 + size + footer


__all__ = [
    "DTS_SYNC",
    "MAX_ID3_TAGS",
    "MIN_FRAMES",
    "FrameLength",
    "ac3_frame_length",
    "adts_frame_length",
    "dts_frame_length",
    "frames_follow",
    "mpeg_audio_frame_length",
    "skip_id3v2",
]
