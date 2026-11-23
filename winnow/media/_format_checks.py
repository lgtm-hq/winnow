"""Header checks for less common library formats and playlists (#393).

The issue's ruling for PR B: a format a photo, video or audio library can
hold gets a signature when it has a distinctive magic of at least four
bytes at a fixed offset. Each check below cites the specification or
reference it follows and adds a structural check wherever the magic is
short or could occur in other data. Playlists are text that points at
media; :func:`playlist` recognises them so they are never typed as media.
"""

from __future__ import annotations

import re
import struct
from typing import Final

from winnow.media._detect_base import Content
from winnow.media._markup import element_named, skip_xml_prolog

PLAYLIST_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        "m3u",
        "m3u8",
        "m4u",
        "mxu",
        "pls",
        "ram",
        "asx",
        "wax",
        "wvx",
        "wmx",
        "wpl",
        "xspf",
        "pla",
        "vlc",
    },
)
"""Playlist extensions: never media, whatever the MIME table says."""


def _u16be(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from(">H", data, offset)[0]
    return value


def _u16le(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<H", data, offset)[0]
    return value


def _u32le(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<I", data, offset)[0]
    return value


# --- images -------------------------------------------------------------------


def psd(content: Content) -> bool:
    """Photoshop PSD (version 1) or PSB (version 2) file header.

    Adobe Photoshop File Formats Specification, File Header Section:
    ``8BPS``, the version, six reserved zero bytes, 1 to 56 channels, a
    non-zero size, a depth of 1, 8, 16 or 32, and a defined color mode.

    Args:
        content: File content.

    Returns:
        Whether the file is PSD or PSB.
    """
    head = content.head
    if len(head) < 26 or head[:4] != b"8BPS" or _u16be(head, 4) not in {1, 2}:
        return False
    fields: tuple[int, ...] = struct.unpack_from(">HIIHH", head, 12)
    channels, height, width, depth, mode = fields
    return (
        head[6:12] == b"\x00" * 6
        and 1 <= channels <= 56
        and height > 0
        and width > 0
        and depth in {1, 8, 16, 32}
        and mode in {0, 1, 2, 3, 4, 7, 8, 9}
    )


def canon_crw(content: Content) -> bool:
    """Canon CRW: a CIFF heap (Canon CIFF specification 1.0r4, 2.1).

    Little-endian byte order ``II``, a 26-byte header length, and the type
    and subtype ``HEAP`` ``CCDR``.

    Args:
        content: File content.

    Returns:
        Whether the file is a Canon CRW.
    """
    head = content.head
    return (
        len(head) >= 14
        and head[:2] == b"II"
        and _u32le(head, 2) == 26
        and head[6:14] == b"HEAPCCDR"
    )


NIFF_MAGIC: Final[bytes] = bytes.fromhex("49494e31")
"""NIFF file magic, written in hex for the spell checker."""

JPEG_XL_SIGNATURE: Final[bytes] = b"\x00\x00\x00\x0cJXL \r\n\x87\n"
"""JPEG XL container signature box (ISO/IEC 18181-2, 3.1)."""

JPEG_XS_SIGNATURE: Final[bytes] = b"\x00\x00\x00\x0cJXS \r\n\x87\n"
"""JPEG XS container signature box (ISO/IEC 21122-3, A.5.1)."""


def jpeg_xl(content: Content) -> bool:
    """JPEG XL container: the signature box then a ``ftyp`` of brand ``jxl ``.

    ISO/IEC 18181-2, 3.1 and 3.2. The bare codestream (``FF 0A``) has only
    a two-byte signature and is left to its extension.

    Args:
        content: File content.

    Returns:
        Whether the file is a JPEG XL container.
    """
    head = content.head
    return head[:12] == JPEG_XL_SIGNATURE and head[16:24] == b"ftypjxl "


def jpeg_xs(content: Content) -> bool:
    """JPEG XS: the container signature box, or SOC then CAP.

    ISO/IEC 21122-3, A.5 (signature box then ``ftyp`` brand ``jxs ``), and
    ISO/IEC 21122-1, A.4 (the codestream's SOC marker ``FF 10`` is followed
    directly by the CAP marker ``FF 50``).

    Args:
        content: File content.

    Returns:
        Whether the file is JPEG XS.
    """
    head = content.head
    if head[:12] == JPEG_XS_SIGNATURE:
        return head[16:24] == b"ftypjxs "
    return head[:4] == b"\xff\x10\xff\x50"


def dpx(content: Content) -> bool:
    """DPX: magic ``SDPX`` or ``XPDS`` and a version string.

    SMPTE 268M, generic file header: the magic number in either byte order,
    and the ASCII version ``V1.0`` or ``V2.0`` at offset 8.

    Args:
        content: File content.

    Returns:
        Whether the file is DPX.
    """
    head = content.head
    return head[:4] in {b"SDPX", b"XPDS"} and head[8:12] in {b"V1.0", b"V2.0"}


def mng(content: Content) -> bool:
    """MNG: the eight-byte signature then the ``MHDR`` chunk.

    MNG specification 1.0, 4.1 and 4.2.1.

    Args:
        content: File content.

    Returns:
        Whether the file is MNG.
    """
    head = content.head
    return head[:8] == b"\x8aMNG\r\n\x1a\n" and head[12:16] == b"MHDR"


def dcx(content: Content) -> bool:
    """DCX: magic 3ADE68B1 then a page table of PCX images.

    ZSoft PCX Technical Reference Manual, DCX: the first page offset points
    inside the file, at a PCX header (manufacturer byte ``0A``).

    Args:
        content: File content.

    Returns:
        Whether the file is DCX.
    """
    head = content.head
    if len(head) < 8 or head[:4] != b"\xb1\x68\xde\x3a":
        return False
    first = _u32le(head, 4)
    if not 8 <= first < content.size:
        return False
    return first >= len(head) or head[first] == 0x0A


def niff(content: Content) -> bool:
    """NIFF: the magic ``49 49 4E 31`` and a version (Navy Image File Format).

    The magic is written in hex: the spell checker rewrites its ASCII form.

    Args:
        content: File content.

    Returns:
        Whether the file is NIFF.
    """
    head = content.head
    return head[:4] == NIFF_MAGIC and len(head) >= 6


def xcursor(content: Content) -> bool:
    """X cursor file: ``Xcur``, a 16-byte header, version 1.0.

    libXcursor ``Xcursor.h``: magic, header size 16, version 0x10000.

    Args:
        content: File content.

    Returns:
        Whether the file is an Xcursor file.
    """
    head = content.head
    return (
        len(head) >= 12
        and head[:4] == b"Xcur"
        and _u32le(head, 4) == 16
        and _u32le(head, 8) == 0x10000
    )


def emf(content: Content) -> bool:
    """Enhanced metafile: an EMR_HEADER record with the EMF signature.

    [MS-EMF] 2.3.4.2: record type 1 at offset 0 and ``ENHMETA_SIGNATURE``
    (`` EMF``) at offset 40.

    Args:
        content: File content.

    Returns:
        Whether the file is EMF.
    """
    head = content.head
    return len(head) >= 44 and _u32le(head, 0) == 1 and head[40:44] == b" EMF"


def wmf(content: Content) -> bool:
    """Windows metafile, placeable or not.

    [MS-WMF] 2.3.2.3: the placeable header ``D7 CD C6 9A`` whose checksum
    is the XOR of its first ten 16-bit words; 2.3.2.2: ``META_HEADER`` with
    type 1 or 2, a header size of 9 words and version 0x100 or 0x300.

    Args:
        content: File content.

    Returns:
        Whether the file is WMF.
    """
    head = content.head
    if head[:4] == b"\xd7\xcd\xc6\x9a" and len(head) >= 22:
        checksum = 0
        for index in range(10):
            checksum ^= _u16le(head, index * 2)
        return checksum == _u16le(head, 20)
    return (
        len(head) >= 6
        and _u16le(head, 0) in {1, 2}
        and _u16le(head, 2) == 9
        and _u16le(head, 4) in {0x100, 0x300}
    )


def quicktime_image(content: Content) -> bool:
    """QuickTime image file: an ``idsc`` or ``idat`` atom first.

    Apple QuickTime File Format, QuickTime image files: size-prefixed atoms;
    the first atom's size must fit in the file.

    Args:
        content: File content.

    Returns:
        Whether the file is a QuickTime image.
    """
    head = content.head
    if len(head) < 8 or head[4:8] not in {b"idsc", b"idat"}:
        return False
    size: int = struct.unpack_from(">I", head, 0)[0]
    return 8 <= size <= content.size


def kodak_kdc(content: Content) -> bool:
    """Kodak DC camera RAW: the maker string at offset 242.

    dcraw and LibRaw ``identify``: ``EASTMAN KODAK COMPANY`` at 0xF2.

    Args:
        content: File content.

    Returns:
        Whether the file is a Kodak KDC of this layout.
    """
    return content.head[242:263] == b"EASTMAN KODAK COMPANY"


# --- audio --------------------------------------------------------------------

_AMR_MAGICS: Final[tuple[bytes, ...]] = (
    b"#!AMR\n",
    b"#!AMR-WB\n",
    b"#!AMR_MC1.0\n",
    b"#!AMR-WB_MC1.0\n",
)
"""RFC 4867, 5: the AMR and AMR-WB storage format magic numbers."""


_AMR_FRAME_TYPES: Final[dict[bytes, frozenset[int]]] = {
    b"#!AMR\n": frozenset({*range(9), 15}),
    b"#!AMR-WB\n": frozenset({*range(10), 14, 15}),
    b"#!AMR_MC1.0\n": frozenset({*range(9), 15}),
    b"#!AMR-WB_MC1.0\n": frozenset({*range(10), 14, 15}),
}
"""Frame types each storage format allows (RFC 4867, 5.3; 3GPP TS 26.101)."""


def amr(content: Content) -> bool:
    """AMR or AMR-WB storage format (RFC 4867, section 5).

    The magic, then (for the multichannel forms) a channel description with
    28 reserved zero bits and 1 to 15 channels, then a frame header whose
    padding bits are zero and whose frame type is defined (5.3).

    Args:
        content: File content.

    Returns:
        Whether the file is AMR.
    """
    head = content.head
    magic = next((m for m in _AMR_MAGICS if head.startswith(m)), None)
    if magic is None:
        return False
    position = len(magic)
    if b"_MC" in magic:
        description = head[position : position + 4]
        if len(description) < 4:
            return False
        channels: int = struct.unpack(">I", description)[0]
        if channels >> 4 or not channels & 0x0F:
            return False
        position += 4
    frame = head[position : position + 1]
    if not frame or frame[0] & 0x83:
        return False
    return (frame[0] >> 3) & 0x0F in _AMR_FRAME_TYPES[magic]


_MOD_TAGS: Final[frozenset[bytes]] = frozenset(
    {b"M.K.", b"M!K!", b"M&K!", b"N.T.", b"FLT4", b"FLT8", b"CD81", b"OKTA", b"OCTA"},
)
_MOD_CHANNELS: Final[re.Pattern[bytes]] = re.compile(rb"[1-9]CHN|[1-3][0-9]CH|TDZ[1-3]")
_MOD_TAG_AT: Final[int] = 1080


def mod(content: Content) -> bool:
    """ProTracker-family module: the format tag at offset 1080.

    ProTracker module format (as documented with ModPlug and libxmp): a
    four-byte tag after the 31 sample headers and the order table; a song
    length of 1 to 128 patterns at offset 950; every order entry (952 to
    1079) a pattern number below 128; and in each 30-byte sample header a
    finetune of at most 15 and a volume of at most 64.

    Args:
        content: File content.

    Returns:
        Whether the file is a MOD module.
    """
    head = content.head
    tag = head[_MOD_TAG_AT : _MOD_TAG_AT + 4]
    if len(tag) < 4 or not 1 <= head[950] <= 128:
        return False
    if not (tag in _MOD_TAGS or _MOD_CHANNELS.fullmatch(tag) is not None):
        return False
    if max(head[952:1080]) >= 128:
        return False
    for sample in range(31):
        header = 20 + sample * 30
        if head[header + 24] > 15 or head[header + 25] > 64:
            return False
    return True


def scream_tracker_3(content: Content) -> bool:
    """Scream Tracker 3 module (ST3 ``TECH.DOC``).

    ``SCRM`` at offset 44, end-of-file byte ``1A`` at 28, type 16 at 29.

    Args:
        content: File content.

    Returns:
        Whether the file is S3M.
    """
    head = content.head
    return head[44:48] == b"SCRM" and head[28:30] == b"\x1a\x10"


def scream_tracker_2(content: Content) -> bool:
    """Scream Tracker 2 module: a tracker name at 20, then type 2.

    STM format (Scream Tracker 2 ``TECH.DOC``): ``!Scream!``, ``!SCREAM!``
    or ``BMOD2STM`` followed by ``1A``, and file type 2 (module) at 29.

    Args:
        content: File content.

    Returns:
        Whether the file is STM.
    """
    head = content.head
    names = {b"!Scream!\x1a", b"!SCREAM!\x1a", b"BMOD2STM\x1a"}
    return head[20:29] in names and head[29:30] == b"\x02"


def fasttracker(content: Content) -> bool:
    """FastTracker 2 module or instrument.

    XM format (Mr.H, ``xm.txt``): ``Extended Module: `` with ``1A`` at
    offset 37; XI instrument: ``Extended Instrument: `` with ``1A`` at 43.

    Args:
        content: File content.

    Returns:
        Whether the file is XM or XI.
    """
    head = content.head
    if head.startswith(b"Extended Module: "):
        return head[37:38] == b"\x1a"
    return head.startswith(b"Extended Instrument: ") and head[43:44] == b"\x1a"


def tracker_magic(content: Content) -> bool:
    """Other trackers whose magic is four bytes or more at offset 0.

    Impulse Tracker (``ITTECH.TXT``: ``IMPM``), OctaMED (MMD format:
    ``MMD0`` to ``MMD3``), MED (``MED`` and version 2 to 4), MultiTracker
    (``MTM`` and version 0x10), UltraTracker (``MAS_UTrack_V00`` and a
    version 1 to 4), MO3 (``MO3`` and version 0 to 5).

    Args:
        content: File content.

    Returns:
        Whether the file is one of those modules.
    """
    head = content.head
    if head[:4] in {b"IMPM", b"MMD0", b"MMD1", b"MMD2", b"MMD3"}:
        return True
    if head[:4] in {b"MED\x02", b"MED\x03", b"MED\x04", b"MTM\x10"}:
        return True
    if head[:14] == b"MAS_UTrack_V00":
        return head[14:15] in {b"1", b"2", b"3", b"4"}
    return head[:3] == b"MO3" and head[3:4] in {bytes([v]) for v in range(6)}


def sid(content: Content) -> bool:
    """Commodore 64 SID tune (HVSC ``SID_file_format.txt``).

    ``PSID`` or ``RSID``, version 1 to 4, data offset 0x76 or 0x7C.

    Args:
        content: File content.

    Returns:
        Whether the file is a SID tune.
    """
    head = content.head
    if len(head) < 8 or head[:4] not in {b"PSID", b"RSID"}:
        return False
    return 1 <= _u16be(head, 4) <= 4 and _u16be(head, 6) in {0x76, 0x7C}


_PSF_VERSIONS: Final[frozenset[int]] = frozenset(
    {0x01, 0x02, 0x11, 0x12, 0x13, 0x21, 0x22, 0x23, 0x24, 0x25, 0x41},
)
"""PSF version bytes for the consoles the PSF specification lists."""


def psf(content: Content) -> bool:
    """Portable Sound Format (Neill Corlett's PSF specification).

    ``PSF`` and a known version byte; the reserved area and program sizes
    must fit in the file.

    Args:
        content: File content.

    Returns:
        Whether the file is PSF.
    """
    head = content.head
    if len(head) < 16 or head[:3] != b"PSF" or head[3] not in _PSF_VERSIONS:
        return False
    return 16 + _u32le(head, 4) + _u32le(head, 8) <= content.size


def xmf(content: Content) -> bool:
    """Extensible Music Format: ``XMF_`` and version ``1.00`` or ``2.00``.

    MIDI Manufacturers Association RP-030 and RP-043, file header.

    Args:
        content: File content.

    Returns:
        Whether the file is XMF.
    """
    return content.head[:8] in {b"XMF_1.00", b"XMF_2.00"}


# --- video --------------------------------------------------------------------


def bink(content: Content) -> bool:
    """Bink video: ``BIK`` or ``KB2`` and a revision letter.

    RAD Game Tools Bink container (multimedia.cx wiki, Bink Container).

    Args:
        content: File content.

    Returns:
        Whether the file is Bink.
    """
    head = content.head
    if head[:3] == b"BIK":
        return head[3:4] in {b"b", b"d", b"f", b"g", b"h", b"i", b"k"}
    return head[:3] == b"KB2" and head[3:4] in {
        b"a",
        b"d",
        b"f",
        b"g",
        b"h",
        b"i",
        b"j",
        b"k",
    }


def smacker(content: Content) -> bool:
    """Smacker video: ``SMK2`` or ``SMK4`` (multimedia.cx wiki, Smacker).

    Args:
        content: File content.

    Returns:
        Whether the file is Smacker.
    """
    return content.head[:4] in {b"SMK2", b"SMK4"}


def nsv(content: Content) -> bool:
    """Nullsoft Streaming Video: ``NSVf`` file header or ``NSVs`` sync.

    Nullsoft NSV format specification: ``NSVf`` and a header size of at
    least 28 bytes and at most the file size; or ``NSVs``, two format
    codes, width and height, and a non-zero frame-rate byte at offset 16.

    Args:
        content: File content.

    Returns:
        Whether the file is NSV.
    """
    head = content.head
    if head[:4] == b"NSVf" and len(head) >= 8:
        return 28 <= _u32le(head, 4) <= content.size
    return head[:4] == b"NSVs" and len(head) >= 19 and head[16] != 0


def sgi_movie(content: Content) -> bool:
    """Silicon Graphics movie file: ``MOVI`` and a version of 0 to 3.

    The SGI movie library's file header as file(1) describes it
    (``Magdir/sgi``): magic ``MOVI``, then the version byte.

    Args:
        content: File content.

    Returns:
        Whether the file is an SGI movie.
    """
    head = content.head
    return head[:4] == b"MOVI" and head[4:5] in {b"\x00", b"\x01", b"\x02", b"\x03"}


# --- playlists ----------------------------------------------------------------


def playlist(content: Content) -> bool:
    """A playlist: text or a list that points at media, not media itself.

    Extended M3U (``#EXTM3U``, RFC 8216 4.3.1.1) and ``#EXTM4U``; PLS
    (``[playlist]``); ASX (``<asx``); Windows Media Player playlist
    (``<?wpl``); XSPF (``<playlist`` in the ``xspf.org`` namespace);
    iriver PLA (``iriver UMS PLA`` at offset 4).

    Args:
        content: File content.

    Returns:
        Whether the file is a playlist.
    """
    head = content.head
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip()
    if text.startswith((b"#EXTM3U", b"#EXTM4U")) or text[:10].lower() == b"[playlist]":
        return True
    if head[4:18] == b"iriver UMS PLA":
        return True
    return _xml_playlist(head)


def _xml_playlist(head: bytes) -> bool:
    """Recognise an XML playlist after its prolog, in one forward pass.

    Args:
        head: The file's opening bytes.

    Returns:
        Whether the first markup is ``<asx``, ``<?wpl`` or an XSPF
        ``<playlist`` start tag.
    """
    position = skip_xml_prolog(head, instructions=False, doctype=False)
    if position is None:
        return False
    if element_named(head, position, b"asx"):
        return True
    if head[position : position + 5].lower() == b"<?wpl":
        return head[position + 5 : position + 6] in {b" ", b"\t", b"\r", b"\n", b"?"}
    if not element_named(head, position, b"playlist"):
        return False
    tag_end = head.find(b">", position)
    tag = head[position : tag_end if tag_end >= 0 else len(head)]
    return _has_attribute_value(tag, _XSPF_NAMESPACE)


_XSPF_NAMESPACE: Final = b"http://xspf.org/ns/0/"
"""The XSPF namespace name (XSPF version 1, section 4.1.1)."""


def _has_attribute_value(tag: bytes, value: bytes) -> bool:
    """Report whether a start tag holds an attribute with exactly ``value``.

    The tag is split at each quote character, so every second piece is a
    quoted attribute value. A value is compared whole, never searched for.

    Args:
        tag: The start tag's bytes, without the closing ``>``.
        value: The attribute value to look for.

    Returns:
        Whether a double- or single-quoted attribute value equals ``value``.
    """
    return any(
        quoted == value for quote in (b'"', b"'") for quoted in tag.split(quote)[1::2]
    )


__all__ = [
    "JPEG_XL_SIGNATURE",
    "JPEG_XS_SIGNATURE",
    "NIFF_MAGIC",
    "PLAYLIST_EXTENSIONS",
    "amr",
    "bink",
    "canon_crw",
    "dcx",
    "dpx",
    "emf",
    "fasttracker",
    "jpeg_xl",
    "jpeg_xs",
    "kodak_kdc",
    "mng",
    "mod",
    "niff",
    "nsv",
    "playlist",
    "psd",
    "psf",
    "quicktime_image",
    "scream_tracker_2",
    "scream_tracker_3",
    "sgi_movie",
    "sid",
    "smacker",
    "tracker_magic",
    "wmf",
    "xcursor",
    "xmf",
]
