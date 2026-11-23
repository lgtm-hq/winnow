"""Header checks behind the rule 4 signature table (#393).

Each check takes the file's :class:`~winnow.media._detect_base.Content` and
reports whether the bytes have the structure its format's specification
requires; the docstring of each names that specification. The table in
:mod:`winnow.media._signatures` decides the order and what a match means.
:func:`is_text` recognises plain text, which detection treats as non-media.
"""

from __future__ import annotations

import re
import struct
from typing import Final

from winnow.media._containers import mpeg_video_sequence
from winnow.media._detect_base import Content

TEXT_BYTES: Final[int] = 8 * 1024
"""Opening bytes checked when deciding whether a file is plain text."""


def _u16le(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<H", data, offset)[0]
    return value


def _u32le(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<I", data, offset)[0]
    return value


def _u32be(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from(">I", data, offset)[0]
    return value


def bmp(content: Content) -> bool:
    """``BM`` plus a known DIB header size (Microsoft BITMAPFILEHEADER).

    Args:
        content: File content.

    Returns:
        Whether the file is a BMP.
    """
    head = content.head
    return len(head) >= 18 and head[:2] == b"BM" and _u32le(head, 14) in _DIB_SIZES


_DIB_SIZES: Final[frozenset[int]] = frozenset({12, 16, 40, 52, 56, 64, 108, 124})
"""BITMAPCOREHEADER, OS/2 2.x, BITMAPINFOHEADER and its V2 to V5 sizes."""


def ico(content: Content) -> bool:
    """ICONDIR type 1 (icon) or 2 (cursor) with a valid first entry.

    Args:
        content: File content.

    Returns:
        Whether the file is an ICO or CUR.
    """
    head = content.head
    if len(head) < 22 or head[:2] != b"\x00\x00" or _u16le(head, 2) not in {1, 2}:
        return False
    return _u16le(head, 4) >= 1 and head[9] == 0


def pcx(content: Content) -> bool:
    """ZSoft PCX header (PCX Technical Reference Manual, revision 5).

    Args:
        content: File content.

    Returns:
        Whether the file is a PCX.
    """
    head = content.head
    if len(head) < 128 or head[0] != 0x0A or head[1] not in {0, 2, 3, 4, 5}:
        return False
    if head[2] != 1 or head[3] not in {1, 2, 4, 8}:
        return False
    window: tuple[int, ...] = struct.unpack_from("<4H", head, 4)
    xmin, ymin, xmax, ymax = window
    return xmin <= xmax and ymin <= ymax


def sgi(content: Content) -> bool:
    """SGI image header (The SGI Image File Format, version 1.00).

    Args:
        content: File content.

    Returns:
        Whether the file is an SGI image.
    """
    head = content.head
    if len(head) < 12 or head[:2] != b"\x01\xda":
        return False
    dimension: int = struct.unpack_from(">H", head, 4)[0]
    return head[2] in {0, 1} and head[3] in {1, 2} and dimension in {1, 2, 3}


_NETPBM_FIELDS: Final[dict[bytes, int]] = {
    b"P1": 2,
    b"P2": 3,
    b"P3": 3,
    b"P4": 2,
    b"P5": 3,
    b"P6": 3,
}
"""Header numbers after each Netpbm magic: width, height and maxval."""

_NETPBM_TOKEN: Final[re.Pattern[bytes]] = re.compile(rb"(?:\s|#[^\n]*\n)+([0-9]+)")


def netpbm(content: Content) -> bool:
    """PBM, PGM, PPM or PAM header (Netpbm format specifications).

    P1 to P6 need whitespace after the magic and then positive width and
    height (and a maxval of 1 to 65535 where the format has one). P7 (PAM)
    needs ``WIDTH``, ``HEIGHT`` and ``ENDHDR`` header lines.

    Args:
        content: File content.

    Returns:
        Whether the file is a Netpbm image.
    """
    head = content.head
    magic = head[:2]
    if magic == b"P7":
        header = head[:1024]
        return (
            header[2:3] == b"\n"
            and b"\nWIDTH " in header
            and b"\nHEIGHT " in header
            and b"\nENDHDR" in header
        )
    fields = _NETPBM_FIELDS.get(magic)
    if fields is None or head[2:3] not in {b" ", b"\t", b"\n", b"\r", b"#"}:
        return False
    position = 2
    numbers: list[int] = []
    for _ in range(fields):
        token = _NETPBM_TOKEN.match(head, position)
        if token is None:
            return False
        numbers.append(int(token.group(1)))
        position = token.end()
    width, height = numbers[0], numbers[1]
    maxval_ok = fields == 2 or 1 <= numbers[2] <= 65535
    return width > 0 and height > 0 and maxval_ok


_XBM: Final[re.Pattern[bytes]] = re.compile(rb"#define[ \t]+\w*width[ \t]+[0-9]+")


def xbm(content: Content) -> bool:
    """X11 bitmap: ``#define <name>_width <n>`` first (X Window System, XBM).

    Args:
        content: File content.

    Returns:
        Whether the file is an XBM.
    """
    return _XBM.match(content.head) is not None


_XML_SKIPS: Final[tuple[tuple[bytes, bytes], ...]] = (
    (b"<?", b"?>"),
    (b"<!--", b"-->"),
)


def svg(content: Content) -> bool:
    """SVG: an ``svg`` root element (W3C SVG 1.1, 5.1.1).

    An XML declaration, processing instructions, comments and a DOCTYPE
    may come first.

    Args:
        content: File content.

    Returns:
        Whether the document's root element is ``svg``.
    """
    text = content.head[:TEXT_BYTES].removeprefix(b"\xef\xbb\xbf")
    position = 0
    while True:
        position = len(text) - len(text[position:].lstrip())
        rest = text[position:]
        for opener, closer in _XML_SKIPS:
            if rest.startswith(opener):
                end = rest.find(closer)
                if end < 0:
                    return False
                position += end + len(closer)
                break
        else:
            if rest.startswith(b"<!DOCTYPE"):
                end = _doctype_end(rest)
                if end < 0:
                    return False
                position += end
                continue
            return rest[:4] == b"<svg" and rest[4:5] in {
                b" ",
                b"\t",
                b"\n",
                b"\r",
                b">",
                b"/",
            }


def _doctype_end(text: bytes) -> int:
    """Return the offset after a DOCTYPE, internal subset included.

    Args:
        text: Bytes starting with ``<!DOCTYPE``.

    Returns:
        The offset after its closing ``>``, or -1 when it does not close.
    """
    bracket = text.find(b"[")
    close = text.find(b">")
    if 0 <= bracket < close:
        subset = text.find(b"]", bracket)
        close = text.find(b">", subset) if subset >= 0 else -1
    return close + 1 if close >= 0 else -1


def icns(content: Content) -> bool:
    """Apple icon image: ``icns`` and a length within the file.

    Args:
        content: File content.

    Returns:
        Whether the file is an ICNS.
    """
    head = content.head
    return (
        len(head) >= 8 and head[:4] == b"icns" and 8 <= _u32be(head, 4) <= content.size
    )


def dds(content: Content) -> bool:
    """DirectDraw Surface: ``DDS `` and a 124-byte DDS_HEADER (Microsoft).

    Args:
        content: File content.

    Returns:
        Whether the file is a DDS.
    """
    head = content.head
    return len(head) >= 8 and head[:4] == b"DDS " and _u32le(head, 4) == 124


def tga(content: Content) -> bool:
    """Truevision TGA 2.0 footer (Truevision TGA File Format 2.0, footer).

    Args:
        content: File content.

    Returns:
        Whether the file ends with the TGA 2.0 signature.
    """
    return content.tail.endswith(b"TRUEVISION-XFILE.\x00")


def flac(content: Content) -> bool:
    """``fLaC`` then a 34-byte STREAMINFO block (RFC 9639, 8.1 and 8.2).

    Args:
        content: File content.

    Returns:
        Whether the file is a native FLAC stream.
    """
    return flac_at(content.head, 0)


def flac_at(data: bytes, offset: int) -> bool:
    """Check for a FLAC stream marker and STREAMINFO block at ``offset``.

    Args:
        data: Bytes read.
        offset: Offset of the marker.

    Returns:
        Whether a FLAC stream starts there.
    """
    block = data[offset : offset + 8]
    return (
        block[:4] == b"fLaC" and block[4] & 0x7F == 0 and block[5:8] == b"\x00\x00\x22"
    )


def au(content: Content) -> bool:
    """Sun/NeXT audio: ``.snd``, a data offset of at least 24, an encoding.

    Args:
        content: File content.

    Returns:
        Whether the file is an AU file (Sun ``audio_filehdr``).
    """
    head = content.head
    if len(head) < 24 or head[:4] != b".snd":
        return False
    fields: tuple[int, ...] = struct.unpack_from(">5I", head, 4)
    offset, _, encoding, rate, channels = fields
    return offset >= 24 and 1 <= encoding <= 27 and rate > 0 and channels > 0


def caf(content: Content) -> bool:
    """Core Audio Format: ``caff``, version 1, flags 0 (Apple CAF spec).

    Args:
        content: File content.

    Returns:
        Whether the file is a CAF.
    """
    return content.head[:8] == b"caff\x00\x01\x00\x00"


def wavpack(content: Content) -> bool:
    """WavPack block: ``wvpk`` and a version of 0x402 to 0x410.

    WavPack 4 and 5 file format, block header.

    Args:
        content: File content.

    Returns:
        Whether the file is a WavPack stream.
    """
    head = content.head
    return (
        len(head) >= 10
        and head[:4] == b"wvpk"
        and _u32le(head, 4) >= 24
        and 0x402 <= _u16le(head, 8) <= 0x410
    )


def tta(content: Content) -> bool:
    """True Audio: ``TTA1`` and format 1 (PCM) or 2 (encrypted).

    TTA lossless audio format, file header.

    Args:
        content: File content.

    Returns:
        Whether the file is a TTA stream.
    """
    head = content.head
    return len(head) >= 6 and head[:4] == b"TTA1" and _u16le(head, 4) in {1, 2}


def voc(content: Content) -> bool:
    """Creative Voice File header and its version checksum.

    Creative Voice File format: header size 26 and checksum equal to the
    complement of the version plus 0x1234.

    Args:
        content: File content.

    Returns:
        Whether the file is a VOC.
    """
    head = content.head
    if len(head) < 26 or head[:20] != b"Creative Voice File\x1a":
        return False
    version = _u16le(head, 22)
    return _u16le(head, 20) == 26 and _u16le(head, 24) == (~version + 0x1234) & 0xFFFF


def flv(content: Content) -> bool:
    """FLV header: ``FLV``, version 1, data offset of at least 9.

    Adobe Flash Video File Format Specification 10.1, E.2.

    Args:
        content: File content.

    Returns:
        Whether the file is an FLV.
    """
    head = content.head
    return len(head) >= 9 and head[:4] == b"FLV\x01" and _u32be(head, 5) >= 9


def realmedia(content: Content) -> bool:
    """RealMedia file header ``.RMF`` with object version 0 or 1.

    Args:
        content: File content.

    Returns:
        Whether the file is a RealMedia file.
    """
    head = content.head
    return (
        len(head) >= 10
        and head[:4] == b".RMF"
        and head[8:10] in {b"\x00\x00", b"\x00\x01"}
    )


def program_stream(content: Content) -> bool:
    """MPEG program stream pack header (ISO/IEC 13818-1, 2.5.3.3).

    Args:
        content: File content.

    Returns:
        Whether the file starts with an MPEG-1 or MPEG-2 pack header.
    """
    head = content.head
    if head[:4] != b"\x00\x00\x01\xba" or len(head) < 14:
        return False
    return head[4] >> 6 == 0b01 or head[4] >> 4 == 0b0010


def mpeg_video(content: Content) -> bool:
    """MPEG-1/2 video elementary stream sequence header.

    Args:
        content: File content.

    Returns:
        Whether the file starts with a valid sequence header.
    """
    return mpeg_video_sequence(content.head)


def is_text(head: bytes, *, size: int) -> bool:
    """Report whether the opening bytes are plain UTF-8 text.

    Text has no NUL and no C0 control character other than tab, line feed,
    form feed and carriage return, and decodes as UTF-8 (a UTF-8 byte-order
    mark is allowed; a character cut by the end of the window is allowed).

    Args:
        head: The file's opening bytes.
        size: File size in bytes.

    Returns:
        ``True`` for a non-empty plain-text file.
    """
    window = head[:TEXT_BYTES].removeprefix(b"\xef\xbb\xbf")
    if not window or window.translate(None, _TEXT_CONTROLS) != window:
        return False
    try:
        window.decode("utf-8")
    except UnicodeDecodeError as exc:
        cut = len(head) > TEXT_BYTES or size > len(head)
        return (
            cut
            and exc.start >= len(window) - 3
            and exc.reason == "unexpected end of data"
        )
    return True


_TEXT_CONTROLS: Final[bytes] = bytes(
    byte for byte in (*range(0x20), 0x7F) if byte not in {0x09, 0x0A, 0x0C, 0x0D}
)


__all__ = [
    "TEXT_BYTES",
    "au",
    "bmp",
    "caf",
    "dds",
    "flac",
    "flac_at",
    "flv",
    "icns",
    "ico",
    "is_text",
    "mpeg_video",
    "netpbm",
    "pcx",
    "program_stream",
    "realmedia",
    "sgi",
    "svg",
    "tga",
    "tta",
    "voc",
    "wavpack",
    "xbm",
]
