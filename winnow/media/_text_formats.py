"""Text-based media formats for detection rule 4 (#393).

SVG, XBM, XPM, Netpbm's plain formats, EPS and FITS headers are text, and
so can the first 8 KiB of a YUV4MPEG2 video or an AMR file be. The
plain-text rule (:func:`winnow.media._header_checks.is_text`) must neither
hide them nor let a text file pass for a binary format. Two tables here
keep the rules consistent:

* :data:`TEXT_MEDIA_MARKERS`: for a plain-text file that no signature
  recognised, the marker its own extension needs. A text file named
  ``.svg`` is an image only when it opens as XML; ``notes.svg`` holding
  prose is not. Extensions without an entry here (XBM, Netpbm) need their
  full signature; the extension-only formats (EPS, DXF, xfig) need their
  marker (:mod:`winnow.media._extension_only`).
* :data:`TEXT_BASED_CONTAINERS`: signature containers that may match
  text. Any other signature is skipped for a file that is plain text, so
  ``NSVs are great`` or ``MAC & PC notes`` is never media.

Every check here scans forward only (see :mod:`winnow.media._markup`),
so its time is linear in the bytes read.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from winnow.media._detect_base import Content
from winnow.media._markup import (
    element_named,
    skip_c_space,
    skip_space,
    skip_xml_prolog,
)

TEXT_BASED_CONTAINERS: Final[frozenset[str]] = frozenset(
    {
        "svg",
        "xbm",
        "xpm",
        "netpbm",
        "eps",
        "fits",
        "yuv4mpeg",
        "amr",
        "xmf",
        "kodak-kdc",
    },
)
"""Signature containers whose files may be plain text.

A healthy file of each can open with 8 KiB of printable bytes: a text
header (SVG, XBM, XPM, Netpbm, EPS, FITS), a text header then raw samples
in the printable range (YUV4MPEG2), frames whose headers and speech bits
are all printable (AMR), variable-length fields that can be printable
(XMF), or a layout not documented beyond the maker string (Kodak KDC).
Each one's check is strict enough for text: a frame walk, a file length
that must equal the file size, or a magic of 21 bytes at a fixed offset.
"""

_BOM: Final[bytes] = b"\xef\xbb\xbf"
_SVG_NAMESPACE: Final[bytes] = b"http://www.w3.org/2000/svg"


# --- Netpbm -------------------------------------------------------------------

_NETPBM_FIELDS: Final[dict[bytes, int]] = {
    b"P1": 2,
    b"P2": 3,
    b"P3": 3,
    b"P4": 2,
    b"P5": 3,
    b"P6": 3,
}
"""Header numbers after each Netpbm magic: width, height and maxval."""

_NETPBM_DIGITS: Final[int] = 10
"""Longest header number; more than ten digits is not a Netpbm dimension."""

_DIGITS: Final[frozenset[int]] = frozenset(b"0123456789")


def _netpbm_number(head: bytes, position: int) -> tuple[int, int] | None:
    """Read the next Netpbm header number after whitespace and comments.

    Netpbm: whitespace separates the fields and ``#`` starts a comment
    that runs to the end of the line. The scan moves forward only.

    Args:
        head: The file's opening bytes.
        position: Where to start.

    Returns:
        The number and the position after it, or ``None`` when there is no
        separator, no number, or a number of more than ten digits.
    """
    start = position
    while True:
        position = skip_space(head, position)
        if head[position : position + 1] != b"#":
            break
        newline = head.find(b"\n", position)
        if newline < 0:
            return None
        position = newline + 1
    if position == start:
        return None
    end = position
    while end < len(head) and head[end] in _DIGITS:
        end += 1
    if not 0 < end - position <= _NETPBM_DIGITS:
        return None
    return int(head[position:end]), end


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
        token = _netpbm_number(head, position)
        if token is None:
            return False
        number, position = token
        numbers.append(number)
    width, height = numbers[0], numbers[1]
    maxval_ok = fields == 2 or 1 <= numbers[2] <= 65535
    return width > 0 and height > 0 and maxval_ok


# --- XBM ----------------------------------------------------------------------

_XBM_DEFINES: Final[int] = 6
"""Most ``#define`` lines read before the bitmap array (size and hot spot)."""

_XBM_SIZES: Final[tuple[bytes, bytes]] = (b"width", b"height")


def _xbm_defines(head: bytes) -> tuple[dict[bytes, bytes], int] | None:
    """Read the leading ``#define`` lines of an XBM file.

    Leading whitespace and C comments are skipped, and so is anything of
    that kind between the defines. A define is ``#define <name> <digits>``;
    a name of ``width``/``height`` or ending ``_width``/``_height`` gives
    the prefix it is recorded under.

    Args:
        head: The file's opening bytes.

    Returns:
        The size defines as ``{b"width" | b"height": prefix}`` (only those
        with a positive value), and the position after the last define;
        ``None`` when a comment is not closed.
    """
    found: dict[bytes, bytes] = {}
    position = skip_c_space(head, 0)
    for _ in range(_XBM_DEFINES):
        if position is None:
            return None
        if not head.startswith(b"#define", position):
            break
        line_end = _define_end(head, position)
        parts = head[position + len(b"#define") : line_end].split()
        if (
            len(parts) >= 2
            and parts[1].isdigit()
            and len(parts[1]) <= 10
            and int(parts[1])
        ):
            for size in _XBM_SIZES:
                if parts[0] == size:
                    found[size] = b""
                elif parts[0].endswith(b"_" + size):
                    found[size] = parts[0][: -len(size) - 1]
        position = skip_c_space(head, line_end)
    if position is None:
        return None
    return found, position


def _define_end(head: bytes, position: int) -> int:
    """Return where a ``#define`` line's text ends: a newline or ``/*``.

    Args:
        head: The file's opening bytes.
        position: Where the ``#define`` starts.

    Returns:
        The position of the newline or comment opener, or ``len(head)``.
    """
    ends = [
        index
        for index in (head.find(b"\n", position), head.find(b"/*", position))
        if index >= 0
    ]
    return min(ends) if ends else len(head)


def xbm(content: Content) -> bool:
    """X bitmap, X11 or X10 (Xlib ``XReadBitmapFile`` format).

    Width and height defines with the same prefix (in either order, with
    comments or hot-spot defines around them), then a ``char`` (X11) or
    ``short`` (X10) array ``<prefix>_bits[]``.

    Args:
        content: File content.

    Returns:
        Whether the file is an XBM.
    """
    head = content.head
    read = _xbm_defines(head)
    if read is None:
        return False
    found, position = read
    if set(found) != set(_XBM_SIZES) or found[b"width"] != found[b"height"]:
        return False
    prefix = found[b"width"]
    name = prefix + b"_bits" if prefix else b"bits"
    index = head.find(name, position)
    if index < 0:
        return False
    bracket = skip_space(head, index + len(name))
    if head[bracket : bracket + 1] != b"[":
        return False
    line_start = head.rfind(b"\n", position, index) + 1
    declaration = head[max(line_start, position) : index].split()
    return b"char" in declaration or b"short" in declaration


# --- YUV4MPEG2 ----------------------------------------------------------------

_Y4M_MAGIC: Final[bytes] = b"YUV4MPEG2 "
_Y4M_FRAME: Final[bytes] = b"FRAME"
_Y4M_HEADER_LIMIT: Final[int] = 4096
"""Longest stream header line read."""


def yuv4mpeg(content: Content) -> bool:
    """YUV4MPEG2 stream (mjpegtools ``yuv4mpeg(5)``).

    The stream header line: the magic, then space-separated parameters
    among which ``W<width>`` and ``H<height>`` are required; then the first
    frame header, ``FRAME`` and a space or the end of its line.

    Args:
        content: File content.

    Returns:
        Whether the file is a YUV4MPEG2 stream with at least one frame.
    """
    head = content.head
    if not head.startswith(_Y4M_MAGIC):
        return False
    line_end = head.find(b"\n", 0, _Y4M_HEADER_LIMIT)
    if line_end < 0:
        return False
    parameters = head[len(_Y4M_MAGIC) : line_end].split(b" ")
    width = any(p[:1] == b"W" and p[1:].isdigit() for p in parameters)
    height = any(p[:1] == b"H" and p[1:].isdigit() for p in parameters)
    frame = line_end + 1 + len(_Y4M_FRAME)
    return (
        width
        and height
        and head.startswith(_Y4M_FRAME, line_end + 1)
        and head[frame : frame + 1] in {b" ", b"\n"}
    )


# --- SVG, XPM, EPS ------------------------------------------------------------


def svg(content: Content) -> bool:
    """SVG: an ``svg`` root element (W3C SVG 1.1, 5.1.1).

    An XML declaration, processing instructions, comments and a DOCTYPE
    may come first. The root may carry a namespace prefix (``<svg:svg``)
    when its start tag binds that prefix to the SVG namespace (Namespaces
    in XML 1.0, 3).

    Args:
        content: File content.

    Returns:
        Whether the document's root element is ``svg``.
    """
    head = content.head
    position = skip_xml_prolog(head, instructions=True, doctype=True)
    if position is None:
        return False
    if element_named(head, position, b"svg"):
        return True
    if head[position : position + 1] != b"<":
        return False
    name_end = position + 1
    limit = min(len(head), position + 1 + _NAME_LIMIT)
    while name_end < limit and head[name_end] not in _NAME_END:
        name_end += 1
    if name_end >= len(head) or head[name_end] not in _NAME_END:
        return False
    prefix, colon, local = head[position + 1 : name_end].partition(b":")
    if not colon or not prefix or local.lower() != b"svg":
        return False
    tag_end = head.find(b">", name_end)
    tag = head[name_end : tag_end if tag_end >= 0 else len(head)]
    binding = b"xmlns:" + prefix + b"="
    return any(
        binding + quote + _SVG_NAMESPACE + quote in tag for quote in (b'"', b"'")
    )


_NAME_LIMIT: Final[int] = 64
"""Longest prefixed root element name looked at."""

_NAME_END: Final[frozenset[int]] = frozenset(b" \t\r\n>/")


def xpm(content: Content) -> bool:
    """XPM: ``/* XPM */`` (XPM 3) or ``! XPM2`` after optional whitespace.

    XPM manual (version 3.4), 1.2 and 2.

    Args:
        content: File content.

    Returns:
        Whether the file is XPM.
    """
    head = content.head
    position = skip_space(head, len(_BOM) if head.startswith(_BOM) else 0)
    return head.startswith((b"/* XPM */", b"! XPM2"), position)


def _xml_start(content: Content) -> bool:
    """Report whether a text file opens as XML: a declaration or a comment.

    The marker a plain-text ``.svg`` needs when its root element lies past
    the bytes read (a long prolog).

    Args:
        content: File content.

    Returns:
        Whether ``<?xml`` or ``<!--`` comes first, after a BOM and space.
    """
    head = content.head
    position = skip_space(head, len(_BOM) if head.startswith(_BOM) else 0)
    return head.startswith((b"<?xml", b"<!--", b"<!DOCTYPE svg"), position)


def _xpm1(content: Content) -> bool:
    """XPM version 1: ``#define <name>_format 1`` first (XPM manual, 1.2).

    Args:
        content: File content.

    Returns:
        Whether the file opens as XPM 1.
    """
    head = content.head
    line_end = head.find(b"\n", 0, 256)
    first_line = head[: line_end if line_end >= 0 else 256].split()
    return (
        len(first_line) == 3
        and first_line[0] == b"#define"
        and first_line[1].endswith(b"_format")
        and first_line[2] == b"1"
    )


TEXT_MEDIA_MARKERS: Final[dict[str, tuple[str, Callable[[Content], bool]]]] = {
    "svg": ("svg", _xml_start),
    "xpm": ("xpm", _xpm1),
}
"""The marker a plain-text file needs under these extensions to be an image.

Checked only when no signature recognised the file: an SVG whose root lies
past the bytes read, or XPM 1. Every other text-based image needs its
signature (FITS, XBM, Netpbm) or, for the extension-only formats, its
marker.
"""


def text_media_marker(suffix: str, content: Content) -> str | None:
    """Return the format a plain-text file's extension and marker agree on.

    Args:
        suffix: The file's extension, dot included.
        content: File content.

    Returns:
        The format name when the extension has a marker and the file opens
        with it, otherwise ``None``.
    """
    entry = TEXT_MEDIA_MARKERS.get(suffix.lstrip(".").casefold())
    if entry is None:
        return None
    name, marker = entry
    return name if marker(content) else None


def eps(content: Content) -> bool:
    """Encapsulated PostScript (Adobe Technical Note 5002, EPSF 3.0).

    The DSC header line ``%!PS-Adobe-<n> EPSF-<n>`` (section 2), or the
    DOS EPS binary header ``C5 D0 D3 C6`` (section 5). EPS and AI files
    without the ``EPSF`` keyword are typed by their extension.

    Args:
        content: File content.

    Returns:
        Whether the file is EPS.
    """
    head = content.head
    if head.startswith(b"\xc5\xd0\xd3\xc6"):
        return True
    first_line = head[:256].split(b"\n", 1)[0].split(b"\r", 1)[0]
    return first_line.startswith(b"%!PS-Adobe-") and b" EPSF-" in first_line


__all__ = [
    "TEXT_BASED_CONTAINERS",
    "TEXT_MEDIA_MARKERS",
    "eps",
    "netpbm",
    "svg",
    "text_media_marker",
    "xbm",
    "xpm",
    "yuv4mpeg",
]
