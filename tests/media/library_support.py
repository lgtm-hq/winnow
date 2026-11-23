"""Minimal well-formed files of the less common library formats (#393).

Each sample follows its format's specification closely enough to be read
by a tool for that format: headers with their required fields, the
smallest valid body. They are used for detection only.
"""

from __future__ import annotations

import struct
import zlib

from tests.media.signature_support import ogg_page, riff
from winnow.media._format_checks import NIFF_MAGIC
from winnow.models.media import MediaType

IMAGE = MediaType.IMAGE
AUDIO = MediaType.AUDIO
VIDEO = MediaType.VIDEO


def _psd(version: int) -> bytes:
    """Build a 1x1 RGB 8-bit PSD (1) or PSB (2) with raw pixel data.

    Args:
        version: 1 for PSD, 2 for PSB.

    Returns:
        The file.
    """
    header = b"8BPS" + struct.pack(">H", version) + b"\x00" * 6
    header += struct.pack(">HIIHH", 3, 1, 1, 8, 3)
    layers = b"\x00" * (4 if version == 1 else 8)
    return header + b"\x00" * 4 + b"\x00" * 4 + layers + b"\x00\x00" + b"\x80\x40\x20"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """Build a PNG-style chunk with its CRC.

    Args:
        kind: Chunk type.
        data: Chunk data.

    Returns:
        The chunk.
    """
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _mng() -> bytes:
    """Build an empty 1x1 MNG: signature, MHDR, MEND.

    Returns:
        The file.
    """
    mhdr = struct.pack(">7I", 1, 1, 1, 0, 0, 0, 1)
    return b"\x8aMNG\r\n\x1a\n" + _png_chunk(b"MHDR", mhdr) + _png_chunk(b"MEND", b"")


def _box(kind: bytes, payload: bytes) -> bytes:
    """Build an ISO-BMFF style box.

    Args:
        kind: Box type.
        payload: Box data.

    Returns:
        The box.
    """
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _jpeg_xl() -> bytes:
    """Build a JPEG XL container around a minimal codestream box.

    Returns:
        The file.
    """
    ftyp = _box(b"ftyp", b"jxl \x00\x00\x00\x00jxl ")
    return b"\x00\x00\x00\x0cJXL \r\n\x87\n" + ftyp + _box(b"jxlc", b"\xff\x0a\x00\x00")


def _jpeg_xs_box() -> bytes:
    """Build a JPEG XS container around a minimal codestream box.

    Returns:
        The file.
    """
    ftyp = _box(b"ftyp", b"jxs \x00\x00\x00\x00jxs ")
    return (
        b"\x00\x00\x00\x0cJXS \r\n\x87\n"
        + ftyp
        + _box(b"jxsc", b"\xff\x10\xff\x50\x00\x02")
    )


def _dcx() -> bytes:
    """Build a one-page DCX holding a 1x1 PCX.

    Returns:
        The file.
    """
    pcx = bytes([0x0A, 5, 1, 8]) + struct.pack("<4H", 0, 0, 0, 0) + b"\x00" * 116
    table = struct.pack("<I", 4100) + b"\x00" * (4096 - 4)
    return b"\xb1\x68\xde\x3a" + table + pcx + b"\xc1\x00"


def _emf() -> bytes:
    """Build an EMF: EMR_HEADER (108 bytes) then EMR_EOF.

    Returns:
        The file.
    """
    header = struct.pack("<II", 1, 108) + struct.pack("<4i", 0, 0, 0, 0) * 2
    header += b" EMF" + struct.pack("<IIIHH", 0x10000, 128, 2, 1, 0)
    header += struct.pack("<III", 0, 0, 0) + struct.pack("<ii", 1024, 768) * 2
    header = header.ljust(108, b"\x00")
    return header + struct.pack("<IIIII", 14, 20, 0, 16, 20)


def _wmf(placeable: bool) -> bytes:
    """Build a WMF: META_HEADER and META_EOF, optionally placeable.

    Args:
        placeable: Prefix the META_PLACEABLE header.

    Returns:
        The file.
    """
    body = (
        struct.pack("<HHHIHIH", 1, 9, 0x300, 12, 0, 3, 0) + b"\x03\x00\x00\x00\x00\x00"
    )
    if not placeable:
        return body
    words = struct.pack("<IH4hHI", 0x9AC6CDD7, 0, 0, 0, 100, 100, 1440, 0)
    checksum = 0
    for index in range(10):
        checksum ^= struct.unpack_from("<H", words, index * 2)[0]
    return words + struct.pack("<H", checksum) + body


def _mod() -> bytes:
    """Build a one-pattern ProTracker module (4 channels, ``M.K.``).

    Returns:
        The file.
    """
    title = b"song".ljust(20, b"\x00")
    samples = (b"".ljust(22, b"\x00") + b"\x00\x00\x00\x40\x00\x00\x00\x01") * 31
    orders = bytes([1, 127]) + b"\x00" * 128
    return title + samples + orders + b"M.K." + b"\x00" * 1024


def _s3m() -> bytes:
    """Build an empty Scream Tracker 3 module header.

    Returns:
        The file.
    """
    header = b"song".ljust(28, b"\x00") + b"\x1a\x10\x00\x00"
    header += struct.pack("<HHHHHH", 2, 0, 0, 0, 0x1320, 2) + b"SCRM"
    header += bytes([64, 6, 125, 48, 16, 0]) + b"\x00" * 10
    return header + b"\xff" * 32 + b"\xff\xff"


def _stm() -> bytes:
    """Build an empty Scream Tracker 2 module header.

    Returns:
        The file.
    """
    header = (
        b"song".ljust(20, b"\x00") + b"!Scream!\x1a\x02\x02\x15" + bytes([0x60, 0, 64])
    )
    return header.ljust(48, b"\x00") + b"\x00" * (31 * 32) + b"\x63" * 128


def _xm() -> bytes:
    """Build an empty FastTracker 2 module.

    Returns:
        The file.
    """
    head = b"Extended Module: " + b"song".ljust(20, b"\x00") + b"\x1a"
    head += b"winnow".ljust(20, b" ") + struct.pack("<H", 0x104)
    head += struct.pack("<I8H", 276, 1, 0, 4, 1, 0, 1, 6, 125)
    return head + b"\x00" * 256 + struct.pack("<IBHH", 9, 0, 1, 0)


def _xi() -> bytes:
    """Build an empty FastTracker 2 instrument.

    Returns:
        The file.
    """
    head = b"Extended Instrument: " + b"inst".ljust(22, b"\x00") + b"\x1a"
    return head + b"winnow".ljust(20, b" ") + struct.pack("<H", 0x102) + b"\x00" * 224


def _sid() -> bytes:
    """Build a PSID v2 tune with a three-byte C64 program.

    Returns:
        The file.
    """
    head = b"PSID" + struct.pack(">HHHHHHHI", 2, 0x7C, 0, 0x1000, 0x1003, 1, 1, 0)
    head += b"song".ljust(32, b"\x00") * 3 + struct.pack(">HBBH", 0, 0, 0, 0)
    return head + b"\x00\x10" + b"\x60\x60\x60"


def _ogm_video() -> bytes:
    """Build an OGM stream header page then a data page.

    Returns:
        The file.
    """
    header = b"\x01video\x00\x00\x00" + b"DIVX" + b"\x00" * 40
    return ogg_page(header) + ogg_page(b"\x00" * 8, bos=False)


LIBRARY_SAMPLES: list[tuple[str, str, bytes, str, MediaType]] = [
    ("psd", "psd", _psd(1), "psd", IMAGE),
    ("psb", "psb", _psd(2), "psd", IMAGE),
    (
        "crw",
        "crw",
        b"II"
        + struct.pack("<I", 26)
        + b"HEAPCCDR"
        + struct.pack("<II", 0x10002, 0)
        + b"\x00" * 64,
        "canon-crw",
        IMAGE,
    ),
    ("jxl", "jxl", _jpeg_xl(), "jpeg-xl", IMAGE),
    ("jxs", "jxs", _jpeg_xs_box(), "jpeg-xs", IMAGE),
    ("jxs-codestream", "jxs", b"\xff\x10\xff\x50\x00\x02\x00\x00", "jpeg-xs", IMAGE),
    (
        "dpx",
        "dpx",
        (b"SDPX" + struct.pack(">I", 2048) + b"V2.0\x00\x00\x00\x00").ljust(
            2048, b"\x00"
        ),
        "dpx",
        IMAGE,
    ),
    ("dcx", "dcx", _dcx(), "dcx", IMAGE),
    ("niff", "nif", NIFF_MAGIC + struct.pack("<H", 1) + b"\x00" * 34, "niff", IMAGE),
    (
        "xcursor",
        "xcur",
        b"Xcur" + struct.pack("<III", 16, 0x10000, 0),
        "xcursor",
        IMAGE,
    ),
    ("emf", "emf", _emf(), "emf", IMAGE),
    ("wmf-placeable", "wmf", _wmf(placeable=True), "wmf", IMAGE),
    ("wmf", "wmf", _wmf(placeable=False), "wmf", IMAGE),
    (
        "qtif",
        "qtif",
        _box(b"idsc", b"jpeg" + b"\x00" * 78) + _box(b"idat", b"\xff\xd8\xff\xd9"),
        "quicktime-image",
        IMAGE,
    ),
    (
        "kdc",
        "kdc",
        b"\x00" * 242 + b"EASTMAN KODAK COMPANY" + b"\x00" * 64,
        "kodak-kdc",
        IMAGE,
    ),
    ("mng", "mng", _mng(), "mng", VIDEO),
    ("amr", "amr", b"#!AMR\n" + (b"\x3c" + b"\x00" * 31) * 3, "amr", AUDIO),
    ("amr-wb", "awb", b"#!AMR-WB\n" + (b"\x44" + b"\x00" * 60) * 3, "amr", AUDIO),
    (
        "qcelp",
        "qcp",
        riff(b"RIFF", b"QLCM", b"fmt " + struct.pack("<I", 150) + b"\x00" * 150),
        "qcelp",
        AUDIO,
    ),
    (
        "8svx",
        "8svx",
        b"FORM"
        + struct.pack(">I", 36)
        + b"8SVXVHDR"
        + struct.pack(">I", 20)
        + b"\x00" * 20
        + b"BODY"
        + struct.pack(">I", 0),
        "8svx",
        AUDIO,
    ),
    ("mod", "mod", _mod(), "mod", AUDIO),
    ("s3m", "s3m", _s3m(), "s3m", AUDIO),
    ("stm", "stm", _stm(), "stm", AUDIO),
    ("xm", "xm", _xm(), "fasttracker", AUDIO),
    ("xi", "xi", _xi(), "fasttracker", AUDIO),
    (
        "it",
        "it",
        b"IMPM" + b"song".ljust(26, b"\x00") + b"\x00" * 162,
        "tracker",
        AUDIO,
    ),
    ("med", "med", b"MMD0" + struct.pack(">I", 52) + b"\x00" * 44, "tracker", AUDIO),
    (
        "mtm",
        "mtm",
        b"MTM\x10" + b"song".ljust(20, b"\x00") + b"\x00" * 42,
        "tracker",
        AUDIO,
    ),
    (
        "ult",
        "ult",
        b"MAS_UTrack_V004" + b"song".ljust(32, b"\x00") + b"\x00" * 16,
        "tracker",
        AUDIO,
    ),
    ("mo3", "mo3", b"MO3\x05" + struct.pack("<I", 16) + b"\x00" * 16, "tracker", AUDIO),
    ("sid", "sid", _sid(), "sid", AUDIO),
    ("psf", "psf", b"PSF\x01" + struct.pack("<III", 0, 0, 0), "psf", AUDIO),
    ("xmf", "xmf", b"XMF_2.00" + b"\x00" * 24, "xmf", AUDIO),
    ("bink", "bik", b"BIKi" + struct.pack("<I", 44) + b"\x00" * 36, "bink", VIDEO),
    (
        "smacker",
        "smk",
        b"SMK2" + struct.pack("<III", 1, 1, 1) + b"\x00" * 88,
        "smacker",
        VIDEO,
    ),
    (
        "nsv",
        "nsv",
        b"NSVf" + struct.pack("<III", 28, 28, 0) + b"\x00" * 12,
        "nsv",
        VIDEO,
    ),
    ("ogm", "ogm", _ogm_video(), "ogg", VIDEO),
    ("sgi-movie", "movie", b"MOVI\x03" + b"\x00" * 59, "sgi-movie", VIDEO),
]
"""``(name, extension, bytes, container, media type)`` of each sample."""

PLAYLIST_SAMPLES: list[tuple[str, str, bytes]] = [
    ("m3u", "m3u", b"#EXTM3U\n#EXTINF:1,Song\nsong.mp3\n"),
    ("m3u-plain", "m3u", b"music/song.mp3\nmusic/other.flac\n"),
    ("m3u8", "m3u8", b"\xef\xbb\xbf#EXTM3U\n#EXT-X-VERSION:3\nseg0.ts\n"),
    ("pls", "pls", b"[playlist]\nFile1=song.mp3\nNumberOfEntries=1\nVersion=2\n"),
    ("asx", "asx", b'<ASX version="3.0"><Entry><Ref href="a.wma"/></Entry></ASX>\n'),
    (
        "xspf",
        "xspf",
        b'<?xml version="1.0"?>\n<playlist version="1" xmlns="http://xspf.org/ns/0/">'
        b"<trackList/></playlist>\n",
    ),
    ("wpl", "wpl", b'<?wpl version="1.0"?>\n<smil><body><seq/></body></smil>\n'),
    ("pla", "pla", b"\x00\x00\x00\x01iriver UMS PLA" + b"\x00" * 498),
    ("ram", "ram", b"rtsp://example.invalid/song.ra\n"),
]
"""``(name, extension, bytes)`` of each playlist sample."""
