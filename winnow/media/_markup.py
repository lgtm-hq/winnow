"""Linear scanners for the start of text formats (#393).

SVG, the XML playlists and XBM may open with whitespace, comments,
processing instructions or a DOCTYPE before the part a signature checks.
Regular expressions that skip "any number of comments or whitespace" can
match the same text in many ways and backtrack exponentially on hostile
input (``--><!--`` or ``*//*`` repeated). These scanners skip that prefix
in one forward pass instead: every step moves the position forward, and
each ``find`` starts where the previous one stopped, so the time is linear
in the bytes scanned.
"""

from __future__ import annotations

from typing import Final

_WHITESPACE: Final[frozenset[int]] = frozenset(b" \t\r\n\f\v")
_BOM: Final[bytes] = b"\xef\xbb\xbf"


def skip_space(data: bytes, position: int) -> int:
    """Return the first position at or after ``position`` that is not space.

    Args:
        data: Bytes to scan.
        position: Where to start.

    Returns:
        The position of the first non-whitespace byte, or ``len(data)``.
    """
    while position < len(data) and data[position] in _WHITESPACE:
        position += 1
    return position


def _past(data: bytes, position: int, opener: bytes, closer: bytes) -> int | None:
    """Return the position after ``closer`` when ``opener`` starts here.

    Args:
        data: Bytes to scan.
        position: Where ``opener`` would start.
        opener: Opening delimiter.
        closer: Closing delimiter.

    Returns:
        The position after the closer, ``-1`` when the opener is not here,
        or ``None`` when the closer never comes.
    """
    if not data.startswith(opener, position):
        return -1
    end = data.find(closer, position + len(opener))
    return None if end < 0 else end + len(closer)


def skip_xml_prolog(
    data: bytes,
    *,
    instructions: bool,
    doctype: bool,
) -> int | None:
    """Skip a byte-order mark, whitespace, comments and an XML prolog.

    W3C XML 1.0, 2.8: the prolog holds the XML declaration, comments,
    processing instructions and the document type declaration.

    Args:
        data: The document's opening bytes.
        instructions: Skip every processing instruction; otherwise only
            the XML declaration ``<?xml ...?>``.
        doctype: Skip a ``<!DOCTYPE ...>``, internal subset included.

    Returns:
        The position of the first other markup or text, or ``None`` when a
        comment, instruction or DOCTYPE is not closed in ``data``.
    """
    position = len(_BOM) if data.startswith(_BOM) else 0
    while True:
        position = skip_space(data, position)
        after = _past(data, position, b"<!--", b"-->")
        if after == -1 and (instructions or data.startswith(b"<?xml", position)):
            after = _past(data, position, b"<?", b"?>")
        if after == -1 and doctype:
            after = _past_doctype(data, position)
        if after is None:
            return None
        if after == -1:
            return position
        position = after


def _past_doctype(data: bytes, position: int) -> int | None:
    """Return the position after a DOCTYPE starting at ``position``.

    Args:
        data: Bytes to scan.
        position: Where ``<!DOCTYPE`` would start.

    Returns:
        The position after it, ``-1`` when no DOCTYPE starts here, or
        ``None`` when it is not closed.
    """
    if not data.startswith(b"<!DOCTYPE", position):
        return -1
    close = data.find(b">", position)
    bracket = data.find(b"[", position, close if close >= 0 else len(data))
    if bracket >= 0:
        subset_end = data.find(b"]", bracket)
        close = data.find(b">", subset_end) if subset_end >= 0 else -1
    return None if close < 0 else close + 1


def skip_c_space(data: bytes, position: int) -> int | None:
    """Skip whitespace and C comments (``/* ... */``) from ``position``.

    Args:
        data: Bytes to scan.
        position: Where to start.

    Returns:
        The position of the first other byte, or ``None`` when a comment
        is not closed in ``data``.
    """
    while True:
        position = skip_space(data, position)
        after = _past(data, position, b"/*", b"*/")
        if after is None:
            return None
        if after == -1:
            return position
        position = after


def element_named(data: bytes, position: int, name: bytes) -> bool:
    """Report whether a start tag ``<name`` begins at ``position``.

    The name matches without regard to ASCII case and must end there
    (whitespace, ``>`` or ``/`` follow).

    Args:
        data: Bytes to scan.
        position: Where ``<`` would be.
        name: Element name.

    Returns:
        Whether the start tag is there.
    """
    end = position + 1 + len(name)
    if (
        data[position : position + 1] != b"<"
        or data[position + 1 : end].lower() != name
    ):
        return False
    follow = data[end : end + 1]
    return follow in {b">", b"/"} or (bool(follow) and follow[0] in _WHITESPACE)


__all__ = ["element_named", "skip_c_space", "skip_space", "skip_xml_prolog"]
