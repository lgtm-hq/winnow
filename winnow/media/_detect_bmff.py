"""Rule 2 of media-type detection: ISO-BMFF and QuickTime (#393).

A file is claimed on structure only. A first box of type ``ftyp`` is
claimed when its declared size is 0, 1 (a 64-bit size follows) or no larger
than an ``ftyp`` within :data:`MAX_FTYP_PAYLOAD_BYTES`; a truncated or short
``ftyp`` is still claimed. A first box of type ``moov``, ``mdat``, ``free``,
``skip``, ``wide`` or ``pnot``, or of the media-segment types ``styp``,
``sidx`` and ``moof``, is claimed only when its declared size is plausible:
0; 8 up to the file size; or 1 with a 64-bit size of 16 up to the file
size. Text that happens to hold one of those words at offset 4 is not
QuickTime.

A bounded walk over box headers then reads the ``ftyp`` brands, every
``moov/trak/mdia/hdlr`` handler type, and Canon's ``CNCV`` profile, seeking
past payloads; it never reads a media payload such as ``mdat``. The walk
keeps looking for an ``ftyp`` after ``moov``, so box order does not change
the answer. Once ``moov`` is read and no ``ftyp`` header has been seen, a
malformed tail ends the walk without failing (legacy QuickTime).

A standalone media segment (``styp``, ``sidx`` or ``moof`` first, no
``moov``) names no track type: it is unknown, and its extension does not
replace that answer.

A container claimed here but unresolved, malformed or over budget is
unknown (``media_type=None``) with a signal that says why.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, Final

from winnow.media._detect_base import (
    DECISION_PREFIX,
    BudgetExceededError,
    MalformedError,
    Outcome,
    Reader,
    TypeSignal,
)
from winnow.models.media import MediaType

MAX_BYTES_READ: Final[int] = 256 * 1024
"""Bytes rule 2 may read in total: the opening bytes, headers and payloads."""

MAX_BOXES: Final[int] = 4096
"""Box headers the ISO-BMFF walk may visit, at all depths together."""

MAX_BOX_DEPTH: Final[int] = 8
"""Deepest box nesting the ISO-BMFF walk descends into (top level is 0)."""

MAX_FTYP_PAYLOAD_BYTES: Final[int] = 4096
"""Largest ``ftyp`` payload read; a larger valid box is over budget."""

CANON_PROFILE_BYTES: Final[int] = 64
"""Bytes read from the start of a Canon ``CNCV`` payload."""

HEAD_BYTES: Final[int] = 16
"""Opening bytes read to decide whether rule 2 claims the file."""

_BOX_HEADER: Final[int] = 8
_LARGE_BOX_HEADER: Final[int] = 16
_FTYP_FIXED_PAYLOAD: Final[int] = 8
_BRAND_BYTES: Final[int] = 4
_HANDLER_PAYLOAD: Final[int] = 12
_USERTYPE_BYTES: Final[int] = 16

_FTYP_CLAIM_LIMIT: Final[int] = _BOX_HEADER + MAX_FTYP_PAYLOAD_BYTES
"""Largest 32-bit ``ftyp`` size that claims a file."""

_FTYP: Final[bytes] = b"ftyp"
_MOOV: Final[bytes] = b"moov"
TRACK_BOX: Final[bytes] = bytes.fromhex("7472616b")
"""The track box type (ISO/IEC 14496-12, 8.3.1).

Written in hex: the ``typos`` spell checker rewrites its ASCII spelling.
"""
_MDIA: Final[bytes] = b"mdia"
_HDLR: Final[bytes] = b"hdlr"
_UUID: Final[bytes] = b"uuid"
_CNCV: Final[bytes] = b"CNCV"

_SEGMENT_TYPES: Final[frozenset[bytes]] = frozenset({b"styp", b"sidx", b"moof"})
"""Media-segment boxes (ISO/IEC 14496-12, 8.16) that open a segment file."""

_SIZED_OPENING_TYPES: Final[frozenset[bytes]] = frozenset(
    {_MOOV, b"mdat", b"free", b"skip", b"wide", b"pnot", *_SEGMENT_TYPES},
)
"""Box types that open a claimed file only when their size is plausible."""

_TRACK_HANDLER_PATH: Final[tuple[bytes, ...]] = (_MOOV, TRACK_BOX, _MDIA, _HDLR)
"""The only box path whose ``hdlr`` names a track's media type."""

_CONTAINER_TYPES: Final[frozenset[bytes]] = frozenset({_MOOV, TRACK_BOX, _MDIA})
"""Boxes whose children the walk visits wherever they occur."""

CANON_UUID: Final[bytes] = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
"""Usertype of the ``moov/uuid`` box holding Canon's ``CNCV`` profile.

Reverse-engineered: https://github.com/lclevy/canon_cr3 (not published by
Canon).
"""

HEIF_IMAGE_BRANDS: Final[frozenset[str]] = frozenset(
    {"heic", "heix", "heim", "heis", "avci", "jpeg"},
)
"""Registered HEIF still-image and collection brands (ISO/IEC 23008-12)."""

STRUCTURAL_IMAGE_BRANDS: Final[frozenset[str]] = frozenset({"mif1", "mif2", "miaf"})
"""Brands naming the HEIF/MIAF image family, not a codec."""

AVIF_IMAGE_BRANDS: Final[frozenset[str]] = frozenset({"avif"})
"""AVIF still-image brand (AV1 Image File Format)."""

IMAGE_SEQUENCE_BRANDS: Final[frozenset[str]] = frozenset(
    {"msf1", "hevc", "hevx", "hevm", "hevs", "avis"},
)
"""HEIF and AVIF image-sequence brands; still classified as image."""

AUDIO_BRANDS: Final[frozenset[str]] = frozenset(
    {"M4A ", "M4B ", "M4P ", "F4A ", "F4B "},
)
"""Audio-oriented brands: hints only, never proof of no video track."""

CANON_CRX_BRAND: Final[str] = "crx "
"""Canon CRX family brand, shared by CR3 stills and CRM movies."""

_CANON_CR3_PREFIX: Final[bytes] = b"CanonCR3_"
_CANON_CRM_PREFIX: Final[bytes] = b"CanonCRM"

_VIDEO_HANDLER: Final[str] = "vide"
_SOUND_HANDLER: Final[str] = "soun"
_HANDLER_TYPES: Final[dict[str, MediaType]] = {
    _VIDEO_HANDLER: MediaType.VIDEO,
    _SOUND_HANDLER: MediaType.AUDIO,
}


@dataclass(frozen=True, slots=True)
class _Box:
    """One box header.

    Args:
        kind: Four-byte box type.
        offset: File offset of the box.
        payload: File offset of the payload (after size, type, usertype).
        end: File offset just past the box.
        usertype: The 16-byte usertype of a ``uuid`` box.
    """

    kind: bytes
    offset: int
    payload: int
    end: int
    usertype: bytes | None = None


@dataclass(slots=True)
class _BmffState:
    """What the ISO-BMFF walk has found so far.

    ``saw_ftyp`` is set as soon as a top-level box header names ``ftyp``,
    before the box is validated; ``has_ftyp`` once its brands are read.
    """

    major_brand: str | None = None
    compatible_brands: tuple[str, ...] = ()
    saw_ftyp: bool = False
    has_ftyp: bool = False
    has_moov: bool = False
    segment: bool = False
    handlers: list[str] = field(default_factory=list)
    canon_profile: bytes | None = None

    @property
    def brands(self) -> frozenset[str]:
        """Return the major and compatible brands as an unordered set.

        Returns:
            All brands; empty without an ``ftyp``.
        """
        if self.major_brand is None:
            return frozenset(self.compatible_brands)
        return frozenset({self.major_brand, *self.compatible_brands})


def inspect_bmff(handle: BinaryIO, size: int) -> Outcome | None:
    """Apply rule 2 when the file opens with an ISO-BMFF or QuickTime box.

    The opening bytes are read through the rule's reader, so they count
    against :data:`MAX_BYTES_READ`.

    Args:
        handle: The open regular file.
        size: File size in bytes.

    Returns:
        The rule's outcome, or ``None`` when rule 2 does not claim the file.
    """
    reader = Reader(handle=handle, size=size, budget=MAX_BYTES_READ)
    state = _BmffState()
    try:
        head = reader.read_at(0, HEAD_BYTES)
        if not _claims(head, reader.size):
            return None
        state.segment = head[4:8] in _SEGMENT_TYPES
        _walk_top_level(reader, state)
    except MalformedError as exc:
        return _bmff_outcome(state, None, f"unknown malformed: {exc}")
    except BudgetExceededError as exc:
        return _bmff_outcome(state, None, f"unknown budget-exceeded: {exc}")
    media_type, reason = _classify_bmff(state)
    return _bmff_outcome(state, media_type, reason)


def _claims(head: bytes, size: int) -> bool:
    """Decide from the first box header whether rule 2 claims the file.

    Args:
        head: The file's first bytes, up to an extended box header.
        size: File size in bytes.

    Returns:
        ``True`` when rule 2 handles the file.
    """
    if len(head) < _BOX_HEADER:
        return False
    kind = head[4:8]
    if kind == _FTYP:
        declared: int = struct.unpack(">I", head[:4])[0]
        return declared <= _FTYP_CLAIM_LIMIT
    if kind not in _SIZED_OPENING_TYPES:
        return False
    return _plausible_size(head, size)


def _plausible_size(head: bytes, size: int) -> bool:
    """Check the declared size of the first box against the file size.

    Args:
        head: The file's first bytes, at least one box header.
        size: File size in bytes.

    Returns:
        ``True`` for 0, for 8 up to ``size``, or for 1 with a 64-bit size of
        16 up to ``size``.
    """
    declared: int = struct.unpack(">I", head[:4])[0]
    if declared == 1:
        if len(head) < _LARGE_BOX_HEADER:
            return False
        large: int = struct.unpack(">Q", head[_BOX_HEADER:_LARGE_BOX_HEADER])[0]
        return _LARGE_BOX_HEADER <= large <= size
    return declared == 0 or _BOX_HEADER <= declared <= size


def _walk_top_level(reader: Reader, state: _BmffState) -> None:
    """Visit top-level boxes until the brands and tracks are known.

    Payloads are skipped by seeking, so ``moov`` after a large ``mdat`` is
    found without reading the ``mdat``, and an ``ftyp`` after ``moov`` is
    still read.

    Args:
        reader: Reader over the open file.
        state: Walk state to fill in.

    Raises:
        MalformedError: On a malformed box, unless ``moov`` has been read
            and no ``ftyp`` header was seen (legacy QuickTime).
    """
    boxes = _iter_boxes(
        reader,
        start=0,
        end=reader.size,
        depth=0,
        on_kind=lambda kind: _note_top_level(state, kind),
    )
    try:
        for box in boxes:
            if box.kind == _FTYP and not state.has_ftyp:
                _read_ftyp(reader, box, state)
            elif box.kind == _MOOV and not state.has_moov:
                state.has_moov = True
                _walk_children(reader, box, state, path=(_MOOV,))
            if _walk_done(state):
                return
    except MalformedError:
        if state.has_moov and not state.saw_ftyp:
            return
        raise


def _note_top_level(state: _BmffState, kind: bytes) -> None:
    """Record a top-level box type as soon as its header names it.

    Args:
        state: Walk state.
        kind: Four-byte box type.
    """
    if kind == _FTYP:
        state.saw_ftyp = True


def _walk_done(state: _BmffState) -> bool:
    """Report whether the walk has seen enough to decide.

    Args:
        state: Walk state.

    Returns:
        ``True`` once both ``ftyp`` and ``moov`` are read, or once ``ftyp``
        alone decides an image.
    """
    if not state.has_ftyp:
        return False
    if state.has_moov:
        return True
    brands = state.brands
    return (
        CANON_CRX_BRAND not in brands
        and bool(brands & _image_brands())
        and not brands & AUDIO_BRANDS
    )


def _image_brands() -> frozenset[str]:
    """Return every brand that places a file in the image family.

    Returns:
        HEIF, structural, AVIF and image-sequence brands together.
    """
    return (
        HEIF_IMAGE_BRANDS
        | STRUCTURAL_IMAGE_BRANDS
        | AVIF_IMAGE_BRANDS
        | IMAGE_SEQUENCE_BRANDS
    )


def _iter_boxes(
    reader: Reader,
    *,
    start: int,
    end: int,
    depth: int,
    on_kind: Callable[[bytes], None] | None = None,
) -> Iterator[_Box]:
    """Yield the boxes between ``start`` and ``end``, reading headers only.

    Args:
        reader: Reader over the open file.
        start: Offset of the first box.
        end: Offset just past the last box (the parent's end or file end).
        depth: Nesting depth of these boxes.
        on_kind: Called with each box type as soon as it is read, before
            the header is validated.

    Yields:
        Each box header in order.

    Raises:
        BudgetExceededError: Past ``MAX_BOX_DEPTH`` or ``MAX_BOXES``.
    """
    if depth > MAX_BOX_DEPTH:
        raise BudgetExceededError("box depth")
    offset = start
    while offset < end:
        reader.boxes += 1
        if reader.boxes > MAX_BOXES:
            raise BudgetExceededError("box count")
        # A box is never smaller than its header, so each step moves forward.
        box = _read_box_header(reader, offset=offset, end=end, on_kind=on_kind)
        yield box
        offset = box.end


def _read_box_header(
    reader: Reader,
    *,
    offset: int,
    end: int,
    on_kind: Callable[[bytes], None] | None = None,
) -> _Box:
    """Parse the box header at ``offset``.

    Args:
        reader: Reader over the open file.
        offset: Offset of the box.
        end: Offset the box may not pass.
        on_kind: Called with the box type before the header is validated.

    Returns:
        The parsed header.

    Raises:
        MalformedError: On a truncated header, a size smaller than its
            header, or a box past ``end``.
    """
    if end - offset < _BOX_HEADER:
        raise MalformedError("truncated box header")
    raw = reader.read_exact(offset, _BOX_HEADER, "box header")
    (size,) = struct.unpack(">I", raw[:4])
    kind = raw[4:8]
    if on_kind is not None:
        on_kind(kind)
    header = _BOX_HEADER
    if size == 1:
        if end - offset < _LARGE_BOX_HEADER:
            raise MalformedError("truncated extended box header")
        large = reader.read_exact(offset + _BOX_HEADER, 8, "extended box size")
        (size,) = struct.unpack(">Q", large)
        header = _LARGE_BOX_HEADER
    elif size == 0:
        size = end - offset
    if size < header:
        raise MalformedError("non-progressing box: size smaller than its header")
    if offset + size > end:
        where = "the file" if end == reader.size else "its parent"
        raise MalformedError(f"box extends past {where}")
    usertype = None
    if kind == _UUID:
        if size < header + _USERTYPE_BYTES:
            raise MalformedError("uuid box shorter than its usertype")
        usertype = reader.read_exact(offset + header, _USERTYPE_BYTES, "usertype")
        header += _USERTYPE_BYTES
    return _Box(
        kind=kind,
        offset=offset,
        payload=offset + header,
        end=offset + size,
        usertype=usertype,
    )


def _read_ftyp(reader: Reader, box: _Box, state: _BmffState) -> None:
    """Read the brands of an ``ftyp`` box.

    Args:
        reader: Reader over the open file.
        box: The ``ftyp`` box.
        state: Walk state to fill in.

    Raises:
        MalformedError: If the payload is shorter than major brand and
            minor version, or its brand list is not whole brands.
        BudgetExceededError: If the payload is over
            ``MAX_FTYP_PAYLOAD_BYTES``.
    """
    length = box.end - box.payload
    if length < _FTYP_FIXED_PAYLOAD:
        raise MalformedError("ftyp shorter than its minimum size")
    if (length - _FTYP_FIXED_PAYLOAD) % _BRAND_BYTES:
        raise MalformedError("ftyp brand list is not a multiple of four bytes")
    if length > MAX_FTYP_PAYLOAD_BYTES:
        raise BudgetExceededError("ftyp larger than its read budget")
    payload = reader.read_exact(box.payload, length, "ftyp")
    state.has_ftyp = True
    state.major_brand = _brand(payload[:_BRAND_BYTES])
    state.compatible_brands = tuple(
        _brand(payload[index : index + _BRAND_BYTES])
        for index in range(_FTYP_FIXED_PAYLOAD, length, _BRAND_BYTES)
    )


def _brand(raw: bytes) -> str:
    """Decode a four-byte brand, keeping spaces and case.

    Args:
        raw: Four brand bytes.

    Returns:
        The brand, one character per byte.
    """
    return raw.decode("latin-1")


def _walk_children(
    reader: Reader,
    box: _Box,
    state: _BmffState,
    *,
    path: tuple[bytes, ...],
) -> None:
    """Visit the children of a container box.

    Records ``hdlr`` types found at exactly ``moov/trak/mdia/hdlr`` (a
    nested ``moov`` does not count) and the Canon ``CNCV`` profile under
    ``moov/uuid``. Descends into ``moov``, the track box and ``mdia``
    wherever they occur, so malformed nesting hits the depth cap.

    Args:
        reader: Reader over the open file.
        box: Container box.
        state: Walk state to fill in.
        path: Box types from the top level down to ``box``.
    """
    children = _iter_boxes(reader, start=box.payload, end=box.end, depth=len(path))
    for child in children:
        child_path = (*path, child.kind)
        if child.kind in _CONTAINER_TYPES:
            _walk_children(reader, child, state, path=child_path)
        elif child.kind == _HDLR and child_path == _TRACK_HANDLER_PATH:
            state.handlers.append(_read_handler(reader, child))
        elif child.kind == _UUID and path == (_MOOV,) and child.usertype == CANON_UUID:
            _walk_children(reader, child, state, path=child_path)
        elif child.kind == _CNCV and path == (_MOOV, _UUID):
            length = min(child.end - child.payload, CANON_PROFILE_BYTES)
            state.canon_profile = reader.read_at(child.payload, length)


def _read_handler(reader: Reader, box: _Box) -> str:
    """Read the handler type of an ``hdlr`` box.

    Args:
        reader: Reader over the open file.
        box: The ``hdlr`` box.

    Returns:
        The four-character handler type, such as ``"vide"``.

    Raises:
        MalformedError: If the payload is too short to hold it.
    """
    if box.end - box.payload < _HANDLER_PAYLOAD:
        raise MalformedError("hdlr shorter than its handler type")
    payload = reader.read_exact(box.payload, _HANDLER_PAYLOAD, "hdlr")
    return _brand(payload[8:12])


def _classify_bmff(state: _BmffState) -> tuple[MediaType | None, str]:
    """Decide the media type of a completely walked ISO-BMFF file.

    Args:
        state: Finished walk state.

    Returns:
        The media type (``None`` for unknown) and the reason.
    """
    brands = state.brands
    if CANON_CRX_BRAND in brands:
        return _classify_canon(state.canon_profile)
    image = brands & _image_brands()
    audio = brands & AUDIO_BRANDS
    if image and audio:
        return None, "unknown conflict: image and audio brands"
    if image & IMAGE_SEQUENCE_BRANDS:
        return MediaType.IMAGE, "image-sequence brand"
    if image:
        return MediaType.IMAGE, "image brand"
    return _classify_tracks(state)


def _classify_canon(profile: bytes | None) -> tuple[MediaType | None, str]:
    """Decide a Canon CRX file from its ``CNCV`` profile.

    CR3 still and preview tracks use ``hdlr=vide``, so the profile, not the
    track handlers, decides.

    Args:
        profile: Start of the ``CNCV`` payload, if found.

    Returns:
        Image for CR3, video for CRM, otherwise unknown, with the reason.
    """
    if profile is None:
        return None, "unknown insufficient-evidence: crx brand without CNCV"
    if profile.startswith(_CANON_CR3_PREFIX):
        return MediaType.IMAGE, "canon CR3 profile"
    if profile.startswith(_CANON_CRM_PREFIX):
        return MediaType.VIDEO, "canon CRM profile"
    return None, "unknown unsupported: canon profile"


def _classify_tracks(state: _BmffState) -> tuple[MediaType | None, str]:
    """Decide a non-image ISO-BMFF file from its track handlers.

    Args:
        state: Finished walk state.

    Returns:
        Video when any track is ``vide``, audio when none is and at least
        one is ``soun``, otherwise unknown, with the reason.
    """
    if not state.has_moov:
        if state.segment:
            return None, "unknown unresolved: media segment without moov"
        return None, "unknown unresolved: no moov"
    if _VIDEO_HANDLER in state.handlers:
        return MediaType.VIDEO, "track handler vide"
    if _SOUND_HANDLER in state.handlers:
        return MediaType.AUDIO, "track handlers soun without vide"
    return None, "unknown unresolved: no vide or soun track"


def _container_name(state: _BmffState) -> str:
    """Name the container a claimed file was read as.

    Args:
        state: Walk state.

    Returns:
        ``"iso-bmff"`` with an ``ftyp`` header or for a media segment,
        otherwise ``"quicktime"``.
    """
    return "iso-bmff" if state.saw_ftyp or state.segment else "quicktime"


def _bmff_outcome(
    state: _BmffState,
    media_type: MediaType | None,
    reason: str,
) -> Outcome:
    """Build rule 2's outcome with its signals.

    Args:
        state: Walk state, possibly incomplete.
        media_type: Decided media type, ``None`` for unknown.
        reason: Why.

    Returns:
        The outcome.
    """
    signals: list[TypeSignal] = []
    if state.has_ftyp:
        signals.extend(_brand_signals(state))
    elif state.saw_ftyp:
        signals.append(
            TypeSignal(source="ftyp", detail="ftyp=unread", media_type=None),
        )
    signals.extend(
        TypeSignal(
            source="track-handler",
            detail=f"hdlr={handler}",
            media_type=_HANDLER_TYPES.get(handler),
        )
        for handler in state.handlers
    )
    if state.canon_profile is not None:
        signals.append(_canon_signal(state.canon_profile))
    container = _container_name(state)
    signals.append(
        TypeSignal(
            source="ftyp" if state.saw_ftyp else "track-handler",
            detail=f"{DECISION_PREFIX}{container} {reason}",
            media_type=media_type,
        ),
    )
    return Outcome(
        media_type=media_type,
        signals=tuple(signals),
        container=container,
        major_brand=state.major_brand,
        compatible_brands=state.compatible_brands,
    )


def _brand_signals(state: _BmffState) -> list[TypeSignal]:
    """Describe the ``ftyp`` brands.

    Family lists are sorted, so compatible-brand order never changes them.

    Args:
        state: Walk state with an ``ftyp``.

    Returns:
        Signals for the major brand, the compatible brands, and each brand
        family present.
    """
    brands = state.brands
    signals = [
        TypeSignal(
            source="ftyp",
            detail=f"major_brand={state.major_brand}",
            media_type=None,
        ),
        TypeSignal(
            source="ftyp",
            detail=f"compatible_brands={','.join(state.compatible_brands)}",
            media_type=None,
        ),
    ]
    families: tuple[tuple[str, frozenset[str], MediaType | None], ...] = (
        ("heif-image", HEIF_IMAGE_BRANDS | STRUCTURAL_IMAGE_BRANDS, MediaType.IMAGE),
        ("avif-image", AVIF_IMAGE_BRANDS, MediaType.IMAGE),
        ("image-sequence", IMAGE_SEQUENCE_BRANDS, MediaType.IMAGE),
        ("audio-hint", AUDIO_BRANDS, MediaType.AUDIO),
        ("canon-crx", frozenset({CANON_CRX_BRAND}), None),
    )
    for name, members, media_type in families:
        present = sorted(brands & members)
        if present:
            signals.append(
                TypeSignal(
                    source="ftyp",
                    detail=f"brand_family={name} brands={','.join(present)}",
                    media_type=media_type,
                ),
            )
    return signals


def _canon_signal(profile: bytes) -> TypeSignal:
    """Describe a Canon ``CNCV`` profile.

    Args:
        profile: Start of the ``CNCV`` payload.

    Returns:
        A signature signal naming the profile.
    """
    text = profile.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    media_type: MediaType | None = None
    if profile.startswith(_CANON_CR3_PREFIX):
        media_type = MediaType.IMAGE
    elif profile.startswith(_CANON_CRM_PREFIX):
        media_type = MediaType.VIDEO
    return TypeSignal(
        source="signature",
        detail=f"canon_cncv={text}",
        media_type=media_type,
    )


__all__ = [
    "AUDIO_BRANDS",
    "AVIF_IMAGE_BRANDS",
    "CANON_CRX_BRAND",
    "CANON_PROFILE_BYTES",
    "CANON_UUID",
    "HEAD_BYTES",
    "HEIF_IMAGE_BRANDS",
    "IMAGE_SEQUENCE_BRANDS",
    "MAX_BOXES",
    "MAX_BOX_DEPTH",
    "MAX_BYTES_READ",
    "MAX_FTYP_PAYLOAD_BYTES",
    "STRUCTURAL_IMAGE_BRANDS",
    "TRACK_BOX",
    "inspect_bmff",
]
