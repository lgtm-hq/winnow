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
import uuid
from typing import Final

from winnow.media._detect_base import Content
from winnow.media._packet_streams import mpeg_video_sequence

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

    The first ICONDIRENTRY has a zero reserved byte, a positive
    ``dwBytesInRes``, and an image offset past the directory.

    Args:
        content: File content.

    Returns:
        Whether the file is an ICO or CUR.
    """
    head = content.head
    if len(head) < 22 or head[:2] != b"\x00\x00" or _u16le(head, 2) not in {1, 2}:
        return False
    count = _u16le(head, 4)
    return (
        count >= 1
        and head[9] == 0
        and _u32le(head, 14) > 0
        and _u32le(head, 18) >= 6 + 16 * count
    )


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
    if len(head) < _SGI_HEADER or head[:2] != b"\x01\xda":
        return False
    fields: tuple[int, ...] = struct.unpack_from(">3H", head, 4)
    dimension, width, height = fields
    return (
        head[2] in {0, 1}
        and head[3] in {1, 2}
        and dimension in {1, 2, 3}
        and width > 0
        and (height > 0 or dimension == 1)
    )


_SGI_HEADER: Final[int] = 512
"""An SGI file starts with a 512-byte header."""


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
        len(block) == 8
        and block[:4] == b"fLaC"
        and block[4] & 0x7F == 0
        and block[5:8] == b"\x00\x00\x22"
    )


def au(content: Content) -> bool:
    """Sun/NeXT audio: magic, a data offset of at least 24, an encoding.

    Sun ``audio_filehdr``: ``.snd`` with big-endian fields, or ``dns.``
    with little-endian fields as DEC systems wrote it. SoX ``au.c`` also
    reads DEC's own magic, ``.sd`` and NUL (big-endian) or NUL and ``ds.``
    (little-endian), with the same header.

    Args:
        content: File content.

    Returns:
        Whether the file is an AU file.
    """
    head = content.head
    order = {b".snd": ">", b"dns.": "<", b".sd\x00": ">", b"\x00ds.": "<"}.get(head[:4])
    if len(head) < 24 or order is None:
        return False
    fields: tuple[int, ...] = struct.unpack_from(f"{order}5I", head, 4)
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


def monkeys_audio(content: Content) -> bool:
    """Monkey's Audio: ``MAC `` and a file version (APE_DESCRIPTOR/APE_HEADER).

    Monkey's Audio SDK, ``APEInfo.h``: the version is 3.80 to 3.99 as
    3800 to 3990 in old headers, or a later descriptor version.

    Args:
        content: File content.

    Returns:
        Whether the file is Monkey's Audio.
    """
    head = content.head
    return len(head) >= 6 and head[:4] == b"MAC " and 1000 <= _u16le(head, 4) <= 9999


def musepack(content: Content) -> bool:
    """Musepack: SV7 ``MP+`` with stream version 7, or SV8 ``MPCK``.

    Musepack SV7 header (stream version in the low nibble of byte 3) and
    the SV8 specification (``MPCK`` then a two-letter packet key).

    Args:
        content: File content.

    Returns:
        Whether the file is Musepack.
    """
    head = content.head
    if head[:3] == b"MP+" and len(head) >= 4:
        return head[3] & 0x0F == 7
    key = head[4:6]
    return head[:4] == b"MPCK" and len(key) == 2 and key.isalpha() and key.isupper()


_XCF: Final[re.Pattern[bytes]] = re.compile(rb"gimp xcf (file|v[0-9]{3})\x00")


def xcf(content: Content) -> bool:
    """GIMP XCF: ``gimp xcf file`` or ``gimp xcf v<NNN>`` then NUL.

    GIMP ``devel-docs/xcf.txt``, section 2 (the header).

    Args:
        content: File content.

    Returns:
        Whether the file is XCF.
    """
    return _XCF.match(content.head) is not None


def midi(content: Content) -> bool:
    """Standard MIDI File: ``MThd``, header length 6, format 0 to 2.

    MIDI Manufacturers Association, Standard MIDI Files 1.0, header chunk.

    Args:
        content: File content.

    Returns:
        Whether the file is a MIDI file.
    """
    head = content.head
    if len(head) < 10 or head[:4] != b"MThd":
        return False
    return _u32be(head, 4) == 6 and head[8:10] in {
        b"\x00\x00",
        b"\x00\x01",
        b"\x00\x02",
    }


def fits(content: Content) -> bool:
    """FITS: the ``SIMPLE`` keyword with a logical value in column 30.

    FITS Standard 4.0, section 4.4.1.1: the primary header's first card.
    ``T`` marks a conforming file; ``F`` a file that does not conform to
    the standard, which is still a FITS file.

    Args:
        content: File content.

    Returns:
        Whether the file is FITS.
    """
    head = content.head
    return head[:10] == b"SIMPLE  = " and head[29:30] in {b"T", b"F"}


def djvu(content: Content) -> bool:
    """DjVu: ``AT&T`` then an IFF ``FORM`` of a DjVu form type.

    DjVu v3 specification, section 4: ``DJVU`` (single page), ``DJVM``
    (multi-page), ``DJVI`` (shared data) or ``THUM`` (thumbnails).

    Args:
        content: File content.

    Returns:
        Whether the file is DjVu.
    """
    head = content.head
    return head[:8] == b"AT&TFORM" and head[12:16] in {
        b"DJVU",
        b"DJVM",
        b"DJVI",
        b"THUM",
    }


def dv(content: Content) -> bool:
    """Raw DV: a header-section DIF block first (IEC 61834-2, SMPTE 314M).

    The DIF block ID ``1F 07 00`` (section type header, sequence 0, block
    0), then the header byte: ``DSF`` (525/60 or 625/50) and six ones.

    Args:
        content: File content.

    Returns:
        Whether the file is a raw DV stream.
    """
    head = content.head
    return head[:3] == b"\x1f\x07\x00" and head[3:4] in {b"\x3f", b"\xbf"}


WAVE64_RIFF: Final[bytes] = uuid.UUID("66666972-912e-11cf-a5d6-28db04c10000").bytes_le
"""Sony Wave64 ``riff`` chunk GUID (Sony Wave64 specification)."""

WAVE64_WAVE: Final[bytes] = uuid.UUID("65766177-acf3-11d3-8cd1-00c04f8edb8a").bytes_le
"""Sony Wave64 ``wave`` form GUID."""


def wave64(content: Content) -> bool:
    """Sony Wave64: the ``riff`` GUID, a 64-bit size, the ``wave`` GUID.

    Args:
        content: File content.

    Returns:
        Whether the file is Wave64.
    """
    head = content.head
    return head[:16] == WAVE64_RIFF and head[24:40] == WAVE64_WAVE


def qoi(content: Content) -> bool:
    """QOI image: ``qoif``, a non-zero size, 3 or 4 channels, colorspace 0/1.

    The Quite OK Image Format specification 1.0, header.

    Args:
        content: File content.

    Returns:
        Whether the file is QOI.
    """
    head = content.head
    if len(head) < 14 or head[:4] != b"qoif":
        return False
    return (
        _u32be(head, 4) > 0
        and _u32be(head, 8) > 0
        and head[12] in {3, 4}
        and head[13] in {0, 1}
    )


def jng(content: Content) -> bool:
    """JNG: the eight-byte signature then the ``JHDR`` chunk (JNG 1.0, 4).

    Args:
        content: File content.

    Returns:
        Whether the file is JNG.
    """
    head = content.head
    return head[:8] == b"\x8bJNG\r\n\x1a\n" and head[12:16] == b"JHDR"


def modi(content: Content) -> bool:
    """Microsoft Document Imaging (MDI): ``EP*`` NUL and a header offset.

    The MDI header as file(1) (``Magdir/msdos``) records it, a TIFF-like
    layout: the magic, then a little-endian offset inside the file.

    Args:
        content: File content.

    Returns:
        Whether the file is MODI.
    """
    head = content.head
    return (
        len(head) >= 8
        and head[:4] == b"EP*\x00"
        and 8 <= _u32le(head, 4) < content.size
    )


def openraster(content: Content) -> bool:
    """OpenRaster: a ZIP whose first entry is ``mimetype``, stored, holding
    ``image/openraster`` (OpenRaster specification, file layout).

    Args:
        content: File content.

    Returns:
        Whether the file is OpenRaster.
    """
    head = content.head
    if len(head) < 30 or head[:4] != b"PK\x03\x04" or _u16le(head, 8) != 0:
        return False
    name_length = _u16le(head, 26)
    data = 30 + name_length + _u16le(head, 28)
    return (
        head[30 : 30 + name_length] == b"mimetype"
        and head[data : data + 16] == b"image/openraster"
    )


def adif(content: Content) -> bool:
    """AAC Audio Data Interchange Format: ``ADIF`` (ISO/IEC 13818-7, 6.2.1).

    The ``adif_id`` magic; the plain-text guard keeps text that happens to
    start with it from matching.

    Args:
        content: File content.

    Returns:
        Whether the file is ADIF.
    """
    return content.head[:4] == b"ADIF" and len(content.head) >= 8


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
    "WAVE64_RIFF",
    "WAVE64_WAVE",
    "wave64",
    "qoi",
    "openraster",
    "modi",
    "jng",
    "adif",
    "dv",
    "djvu",
    "fits",
    "midi",
    "xcf",
    "musepack",
    "monkeys_audio",
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
    "pcx",
    "program_stream",
    "realmedia",
    "sgi",
    "tga",
    "tta",
    "voc",
    "wavpack",
]
