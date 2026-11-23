"""Text-based media formats for detection rule 4 (#393).

SVG, XBM, XPM, Netpbm's plain formats, EPS and FITS headers are text. The
plain-text rule (:func:`winnow.media._header_checks.is_text`) must neither
hide them nor let a text file pass for a binary format. Two lists here
keep the rules consistent:

* :data:`TEXT_MEDIA_EXTENSIONS`: extensions of text-based media formats.
  When a text file has one of them and no signature recognised it, the
  extension decides, not the plain-text rule.
* :data:`TEXT_BASED_CONTAINERS`: signature containers that may match
  text. Any other signature is skipped for a file that is plain text, so
  ``NSVs are great`` or ``MAC & PC notes`` is never media.

Every check here scans forward only (see :mod:`winnow.media._markup`),
so its time is linear in the bytes read.
"""

from __future__ import annotations

from typing import Final

from winnow.media._detect_base import Content
from winnow.media._markup import (
    element_named,
    skip_c_space,
    skip_space,
    skip_xml_prolog,
)

TEXT_MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        "svg",
        "xbm",
        "xpm",
        "pbm",
        "pgm",
        "ppm",
        "pnm",
        "pam",
        "eps",
        "epsf",
        "epsi",
        "ai",
        "dxf",
        "fig",
        "sk",
        "sk1",
        "ag",
        "fits",
        "fit",
        "fts",
    },
)
"""Extensions of text-based media: the plain-text rule never overrides them."""

TEXT_BASED_CONTAINERS: Final[frozenset[str]] = frozenset(
    {"svg", "xbm", "xpm", "netpbm", "eps", "fits"},
)
"""Signature containers whose files may be plain text."""

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
    "TEXT_MEDIA_EXTENSIONS",
    "eps",
    "netpbm",
    "svg",
    "xbm",
    "xpm",
]
