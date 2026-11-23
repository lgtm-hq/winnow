"""Extension-only formats: typed when extension and marker agree (#393).

Ruling item 2, as amended by the third review: these formats have no magic
detection can rely on alone, so a file is typed only when two weak signals
agree. A file named with one of these extensions is typed only when it
opens with that format's own marker (``%!`` for EPS and AI, ``#FIG`` for
xfig, ``AF11`` at offset 4 for FLIC). A format with no marker at all
(PICT, TGA without a footer, a raw DIB) is typed only when its content is
not plain text and passes the structural check the format allows.
Otherwise the file is unknown, whatever the host's MIME table says:
``plot.fig`` from MATLAB or ``notes.eps`` holding prose is not media.

Detection applies this after rules 2 to 4, so a file another signature
recognises keeps that answer, and before rule 5, so neither puremagic nor
the extension can type a file here without its marker.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from winnow.media._detect_base import BudgetExceededError, Content
from winnow.media._header_checks import is_text
from winnow.models.media import MediaType

Check = Callable[[Content], bool]


@dataclass(frozen=True, slots=True)
class ExtensionOnly:
    """One extension-only format.

    Args:
        name: Format name, reported as the result's container.
        media_type: The type ``main`` gave the format under its extension.
        check: The marker or structural check its content must pass.
        text: Whether the format is text; a binary format's content must
            not be plain text.
    """

    name: str
    media_type: MediaType
    check: Check
    text: bool = False

    def matches(self, content: Content) -> bool:
        """Report whether ``content`` carries this format's marker.

        Args:
            content: File content.

        Returns:
            Whether the check passes (and a binary format is not text).
        """
        if not self.text and is_text(content.head, size=content.size):
            return False
        return self.check(content)


def _starts(*prefixes: bytes) -> Check:
    """Return a check for any of ``prefixes`` at offset 0.

    Args:
        prefixes: Markers.

    Returns:
        The check.
    """
    return lambda content: content.head.startswith(prefixes)


_669_HEADER: Final[int] = 497


def _composer_669(content: Content) -> bool:
    """Composer 669 module: ``if`` (669) or ``JN`` (Extended 669).

    The checks of OpenMPT's ``Load_669.cpp`` (``ValidateHeader``): at most
    64 samples, 128 patterns and a restart position below 128; at most 40
    control characters in the 108-byte message; each of the 128 orders
    below 128 or ``FE``/``FF``, with a tempo of 1 to 15 where it names a
    pattern, any tempo at most 15, and each break row below 64.

    Args:
        content: File content.

    Returns:
        Whether the file opens as a 669 module.
    """
    head = content.head
    if not head.startswith((b"if", b"JN")) or len(head) < _669_HEADER:
        return False
    samples, patterns, restart = head[110], head[111], head[112]
    if samples > 64 or patterns > 128 or restart >= 128:
        return False
    if sum(1 for c in head[2:110] if 0 < c <= 31) > 40:
        return False
    orders, tempos, breaks = head[113:241], head[241:369], head[369:497]
    for order, tempo, row in zip(orders, tempos, breaks, strict=True):
        if 128 <= order < 0xFE or (order < 128 and not tempo):
            return False
        if tempo > 15 or row >= 64:
            return False
    return True


def _flic(content: Content) -> bool:
    """FLIC animation: the magic ``AF11`` (FLI) or ``AF12`` (FLC) at offset 4.

    Autodesk Animator FLIC header: file size, then the 16-bit magic,
    little-endian.

    Args:
        content: File content.

    Returns:
        Whether the file opens as FLIC.
    """
    return content.head[4:6] in {b"\x11\xaf", b"\x12\xaf"}


_OLE: Final[bytes] = bytes.fromhex("d0cf11e0a1b11ae1")
"""Microsoft compound file magic ([MS-CFB] 2.2), shared with Office files."""

_FLASHPIX_ROOT: Final[bytes] = bytes.fromhex("0067615654c1ce11855300aa00a1f95b")
"""Root storage CLSID 56616700-C154-11CE-8553-00AA00A1F95B (FlashPix 1.0.2).

Pillow's ``FpxImagePlugin`` requires the same root CLSID.
"""


def _flashpix(content: Content) -> bool:
    """FlashPix: a compound file whose root storage has the FlashPix CLSID.

    [MS-CFB] 2.2 and 2.6: the sector shift at 0x1E, the first directory
    sector at 0x30; the root entry is the first directory entry and its
    CLSID is at offset 0x50 within it.

    Args:
        content: File content.

    Returns:
        Whether the file is a FlashPix image.
    """
    head = content.head
    if not head.startswith(_OLE) or len(head) < 0x34:
        return False
    shift: int = struct.unpack_from("<H", head, 0x1E)[0]
    if shift not in {9, 12}:
        return False
    sector: int = struct.unpack_from("<I", head, 0x30)[0]
    clsid_at = ((sector + 1) << shift) + 0x50
    if clsid_at + 16 <= len(head):
        clsid = head[clsid_at : clsid_at + 16]
    elif clsid_at + 16 <= content.size:
        try:
            clsid = content.reader.read_at(clsid_at, 16)
        except BudgetExceededError:
            return False
    else:
        return False
    return clsid == _FLASHPIX_ROOT


_DXF_BINARY: Final[bytes] = b"AutoCAD Binary DXF\r\n\x1a\x00"


def _dxf(content: Content) -> bool:
    """DXF: the binary sentinel, or group code 0 then ``SECTION``.

    AutoCAD DXF Reference: an ASCII file is pairs of lines, a group code
    then a value; it opens with ``0`` / ``SECTION`` after any ``999``
    comment pairs. A binary DXF opens with its sentinel.

    Args:
        content: File content.

    Returns:
        Whether the file opens as DXF.
    """
    head = content.head
    if head.startswith(_DXF_BINARY):
        return True
    lines = head[:4096].splitlines()
    index = 0
    while index + 1 < len(lines) and lines[index].strip() == b"999":
        index += 2
    return (
        index + 1 < len(lines)
        and lines[index].strip() == b"0"
        and lines[index + 1].strip() == b"SECTION"
    )


_DIB_HEADER_SIZES: Final[frozenset[int]] = frozenset({12, 40, 52, 56, 64, 108, 124})
"""BITMAPCOREHEADER, BITMAPINFOHEADER and its V2 to V5 successors."""

_DIB_BIT_COUNTS: Final[frozenset[int]] = frozenset({1, 4, 8, 16, 24, 32})


def _dib(content: Content) -> bool:
    """Raw DIB: a bitmap header of a known size with sane fields.

    Microsoft ``BITMAPINFOHEADER`` and ``BITMAPCOREHEADER``: a known header
    size, a non-zero width and height, one plane and 1, 4, 8, 16, 24 or 32
    bits per pixel. The core header's width and height are 16-bit.

    Args:
        content: File content.

    Returns:
        Whether the file opens with such a header.
    """
    head = content.head
    if len(head) < 16:
        return False
    size: int = struct.unpack_from("<I", head, 0)[0]
    if size not in _DIB_HEADER_SIZES:
        return False
    layout = "<HHHH" if size == 12 else "<iiHH"
    width, height, planes, bits = struct.unpack_from(layout, head, 4)
    return width != 0 and height != 0 and planes == 1 and bits in _DIB_BIT_COUNTS


_TGA_IMAGE_TYPES: Final[frozenset[int]] = frozenset({1, 2, 3, 9, 10, 11})
_TGA_ENTRY_BITS: Final[frozenset[int]] = frozenset({15, 16, 24, 32})
_TGA_PIXEL_BITS: Final[frozenset[int]] = frozenset({8, 15, 16, 24, 32})


def _tga(content: Content) -> bool:
    """TGA without a footer: sane header fields (Truevision TGA 2.0).

    Colour-map type 0 or 1; image type 1, 2, 3, 9, 10 or 11; with no
    colour map, a zero colour-map specification, otherwise an entry size of
    15, 16, 24 or 32 bits; a colour-mapped image has a colour map; a
    non-zero width and height; a pixel depth of 8, 15, 16, 24 or 32.

    Args:
        content: File content.

    Returns:
        Whether the header is a sane TGA header.
    """
    head = content.head
    if len(head) < 18:
        return False
    map_type, image_type = head[1], head[2]
    if map_type not in {0, 1} or image_type not in _TGA_IMAGE_TYPES:
        return False
    if map_type == 0 and (any(head[3:8]) or image_type in {1, 9}):
        return False
    if map_type == 1 and head[7] not in _TGA_ENTRY_BITS:
        return False
    width, height = struct.unpack_from("<HH", head, 12)
    return width > 0 and height > 0 and head[16] in _TGA_PIXEL_BITS


_PICT_PREAMBLE: Final[int] = 512
"""Bytes of application header before a PICT file's picture."""

_PICT_VERSIONS: Final[tuple[bytes, ...]] = (b"\x11\x01", b"\x00\x11\x02\xff")


def _pict(content: Content) -> bool:
    """PICT file: a 512-byte header, size and frame, then a version opcode.

    Inside Macintosh: Imaging With QuickDraw, appendix A: the picture size
    (2 bytes) and frame (8 bytes) come first, then version 1 (``11 01``) or
    the version 2 opcode (``00 11 02 FF``).

    Args:
        content: File content.

    Returns:
        Whether the file is laid out as a PICT file.
    """
    return content.head.startswith(_PICT_VERSIONS, _PICT_PREAMBLE + 10)


_EPS_MARKERS: Final[tuple[bytes, ...]] = (b"%!", b"\xc5\xd0\xd3\xc6")


def _eps(content: Content) -> bool:
    """EPS: ``%!`` or the DOS EPS binary header (Adobe TN 5002).

    Args:
        content: File content.

    Returns:
        Whether the file opens as EPS.
    """
    return content.head.startswith(_EPS_MARKERS)


EXTENSION_ONLY: Final[dict[str, ExtensionOnly]] = {
    "669": ExtensionOnly("669", MediaType.AUDIO, _composer_669),
    # Sprint Music Store audio: an ID3v2.3 tag first (puremagic's pattern).
    "koz": ExtensionOnly("audiokoz", MediaType.AUDIO, _starts(b"ID3\x03")),
    "fli": ExtensionOnly("flic", MediaType.VIDEO, _flic),
    "flc": ExtensionOnly("flic", MediaType.VIDEO, _flic),
    # JPEG XL codestream (ISO/IEC 18181-1, 9.1): the signature FF 0A.
    "jxl": ExtensionOnly("jpeg-xl", MediaType.IMAGE, _starts(b"\xff\x0a")),
    "fpx": ExtensionOnly("flashpix", MediaType.IMAGE, _flashpix),
    "eps": ExtensionOnly("eps", MediaType.IMAGE, _eps, text=True),
    "epsf": ExtensionOnly("eps", MediaType.IMAGE, _eps, text=True),
    "epsi": ExtensionOnly("eps", MediaType.IMAGE, _eps, text=True),
    # PostScript-based Illustrator files; PDF-based ones are PDF (non-media).
    "ai": ExtensionOnly("illustrator", MediaType.IMAGE, _starts(b"%!"), text=True),
    # xfig file format (FORMAT3.2): the first line "#FIG <version>".
    "fig": ExtensionOnly("xfig", MediaType.IMAGE, _starts(b"#FIG "), text=True),
    # Skencil and sK1 documents: "##Sketch" and a version.
    "sk": ExtensionOnly("skencil", MediaType.IMAGE, _starts(b"##Sketch"), text=True),
    "sk1": ExtensionOnly("skencil", MediaType.IMAGE, _starts(b"##Sketch"), text=True),
    # Applix Graphics: the "*BEGIN" record.
    "ag": ExtensionOnly("applix", MediaType.IMAGE, _starts(b"*BEGIN"), text=True),
    # AutoCAD DWG: the version string "AC10nn" (Open Design Alliance spec, 2).
    "dwg": ExtensionOnly("dwg", MediaType.IMAGE, _starts(b"AC10")),
    "dxf": ExtensionOnly("dxf", MediaType.IMAGE, _dxf, text=True),
    # No marker: the structural checks below; never plain text.
    "pict": ExtensionOnly("pict", MediaType.IMAGE, _pict),
    "pct": ExtensionOnly("pict", MediaType.IMAGE, _pict),
    "pic": ExtensionOnly("pict", MediaType.IMAGE, _pict),
    "tga": ExtensionOnly("tga", MediaType.IMAGE, _tga),
    "dib": ExtensionOnly("dib", MediaType.IMAGE, _dib),
}
"""Extension-only formats (ruling item 2) by extension."""


def extension_only(suffix: str) -> ExtensionOnly | None:
    """Return the extension-only format a suffix names, if any.

    Args:
        suffix: The file's extension, dot included.

    Returns:
        The format, or ``None``.
    """
    return EXTENSION_ONLY.get(suffix.lstrip(".").casefold())


__all__ = ["EXTENSION_ONLY", "ExtensionOnly", "extension_only"]
