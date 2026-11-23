"""Rule 3 of media-type detection: Matroska and WebM (#393).

A file opening with the EBML magic ``1A 45 DF A3`` is claimed. Its EBML
header is validated against RFC 8794, section 11.2: element IDs and sizes
are variable-size integers, the header fits in the file,
``EBMLReadVersion`` is 1 (a reader gates on it, not on ``EBMLVersion``),
and ``EBMLMaxIDLength`` and ``EBMLMaxSizeLength`` are within the RFC's
limits. Its ``DocType`` must be ``matroska`` or ``webm`` (RFC 9559,
section 4).

Neither the magic nor the DocType says whether the file holds video, so
the track types decide. They are read natively first
(:mod:`winnow.media._matroska_tracks`: the ``TrackType`` of every
``TrackEntry`` in ``Tracks``). Only when that walk cannot settle the type
(no ``Tracks`` found, a complex track, malformed or over-budget input) is
ffprobe asked for the stream types (a video stream that is not cover art
gives video, audio streams only give audio). Without ffprobe the answer is
then unknown with a signal saying the check was unavailable. The signals
record which path decided; the extension never replaces the answer.
"""

from __future__ import annotations

from typing import Final

from winnow.media import _matroska_tracks
from winnow.media._detect_base import Content, Outcome, Reader, decide, evidence
from winnow.media._stream_probe import probe_stream_types

EBML_MAGIC: Final[bytes] = bytes.fromhex("1a45dfa3")
"""EBML header element ID (RFC 8794, section 11.2.1)."""

MAX_EBML_HEADER_BYTES: Final[int] = 4096
"""Largest EBML header payload read; real headers are a few dozen bytes."""

MATROSKA_DOCTYPES: Final[frozenset[str]] = frozenset({"matroska", "webm"})
"""DocType values of Matroska (RFC 9559) and WebM."""

_EBML_READ_VERSION: Final[int] = 0x42F7
_EBML_MAX_ID_LENGTH: Final[int] = 0x42F2
_EBML_MAX_SIZE_LENGTH: Final[int] = 0x42F3
_DOCTYPE: Final[int] = 0x4282
_MAX_ID_BYTES: Final[int] = 4
_MAX_SIZE_BYTES: Final[int] = 8
_CONTAINER: Final[str] = "matroska"
_DEMUXER: Final[str] = "matroska"


class _EbmlError(Exception):
    """The EBML header breaks RFC 8794."""


def inspect_matroska(content: Content) -> Outcome | None:
    """Apply rule 3 when the file opens with the EBML magic.

    Args:
        content: The file's opening bytes and open descriptor.

    Returns:
        The rule's outcome, or ``None`` when the file is not EBML.
    """
    if not content.head.startswith(EBML_MAGIC):
        return None
    try:
        doctype, header_end = _read_doctype(content.head)
    except _EbmlError as exc:
        return decide(_CONTAINER, None, f"unknown malformed: {exc}")
    header = evidence(f"ebml doctype={doctype}")
    if doctype not in MATROSKA_DOCTYPES:
        return decide(
            "ebml",
            None,
            "unknown unsupported: doctype",
            evidence=(header,),
        )
    reader = Reader(
        handle=content.reader.handle,
        size=content.size,
        budget=_matroska_tracks.MAX_BYTES_READ,
    )
    walk = _matroska_tracks.walk_tracks(reader, header_end)
    tracks = evidence(walk.detail, walk.media_type, source="track-handler")
    if walk.settled:
        if walk.media_type is None:
            reason = "unknown unresolved: no audio or video track"
        else:
            reason = f"track types {walk.media_type.value}"
        return decide(_CONTAINER, walk.media_type, reason, evidence=(header, tracks))
    probe = probe_stream_types(content.descriptor, demuxer=_DEMUXER)
    streams = evidence(probe.detail, probe.media_type, source="track-handler")
    if not probe.available:
        reason = "unknown unavailable: tracks unresolved and ffprobe needed"
    elif probe.media_type is None:
        reason = "unknown unresolved: no audio or video stream established"
    else:
        reason = f"stream types {probe.media_type.value} from ffprobe"
    return decide(
        _CONTAINER,
        probe.media_type,
        reason,
        evidence=(header, tracks, streams),
    )


def _read_doctype(head: bytes) -> tuple[str, int]:
    """Validate the EBML header and return its DocType.

    Args:
        head: The file's opening bytes, starting with the EBML magic.

    Returns:
        The DocType string and the file offset just past the header.

    Raises:
        _EbmlError: When the header breaks RFC 8794 or has no DocType.
    """
    offset = len(EBML_MAGIC)
    size, offset = _read_vint(head, offset, strip_marker=True)
    if size is None or size > MAX_EBML_HEADER_BYTES:
        raise _EbmlError("EBML header size")
    end = offset + size
    if end > len(head):
        raise _EbmlError("EBML header past the end of the file")
    doctype: str | None = None
    while offset < end:
        element, offset = _read_vint(head, offset, strip_marker=False)
        length, offset = _read_vint(head, offset, strip_marker=True)
        if element is None or length is None or offset + length > end:
            raise _EbmlError("EBML header element past the header")
        payload = head[offset : offset + length]
        offset += length
        if element == _DOCTYPE:
            doctype = payload.rstrip(b"\x00").decode("ascii", errors="replace")
        else:
            _check_limit(element, payload)
    if doctype is None:
        raise _EbmlError("no DocType")
    return doctype, end


def _check_limit(element: int, payload: bytes) -> None:
    """Check one EBML header version or limit element.

    Args:
        element: Element ID.
        payload: Element payload (an unsigned integer).

    Raises:
        _EbmlError: When a value is outside what RFC 8794 allows readers.
    """
    if element not in {
        _EBML_READ_VERSION,
        _EBML_MAX_ID_LENGTH,
        _EBML_MAX_SIZE_LENGTH,
    }:
        return
    value = int.from_bytes(payload, "big") if payload else 0
    if element == _EBML_READ_VERSION and value != 1:
        raise _EbmlError(f"EBMLReadVersion {value}")
    if element == _EBML_MAX_ID_LENGTH and not 4 <= value <= _MAX_ID_BYTES:
        raise _EbmlError(f"EBMLMaxIDLength {value}")
    if element == _EBML_MAX_SIZE_LENGTH and not 1 <= value <= _MAX_SIZE_BYTES:
        raise _EbmlError(f"EBMLMaxSizeLength {value}")


def _read_vint(
    data: bytes,
    offset: int,
    *,
    strip_marker: bool,
) -> tuple[int | None, int]:
    """Read an EBML variable-size integer (RFC 8794, section 4).

    Args:
        data: Bytes to read from.
        offset: Offset of the first byte.
        strip_marker: Clear the length marker (sizes) or keep it (IDs).

    Returns:
        The value and the offset after it; ``None`` for a truncated or
        invalid integer, or an unknown size (all value bits set).
    """
    if offset >= len(data) or data[offset] == 0:
        return None, offset + 1
    first = data[offset]
    width = 8 - first.bit_length() + 1
    if offset + width > len(data):
        return None, offset + width
    raw = int.from_bytes(data[offset : offset + width], "big")
    if not strip_marker:
        return raw, offset + width
    value_bits = 7 * width
    value = raw & ((1 << value_bits) - 1)
    if value == (1 << value_bits) - 1:
        return None, offset + width
    return value, offset + width


__all__ = [
    "EBML_MAGIC",
    "MATROSKA_DOCTYPES",
    "MAX_EBML_HEADER_BYTES",
    "inspect_matroska",
]
