"""Content-based media-type detection with a structured, explainable result.

:func:`detect` applies these rules in order; the first that decides wins
(#393):

1. **User override.** An extension the user registered on the
   :class:`~winnow.media.registry.FormatRegistry` decides.
2. **ISO-BMFF and QuickTime.** Claimed on structure only: a first box of
   type ``ftyp`` always, a first box of type ``moov``, ``mdat``, ``free``,
   ``skip``, ``wide`` or ``pnot`` only when its declared size is plausible
   (0; 8 up to the file size; or 1 with a 64-bit size of 16 up to the file
   size). A bounded walk over box headers then reads the ``ftyp`` brands,
   every ``moov/trak/mdia/hdlr`` handler type, and Canon's ``CNCV``
   profile. It never reads a media payload such as ``mdat``. A legacy
   QuickTime file, one whose ``moov`` comes before any ``ftyp``, is decided
   as soon as its ``moov`` is read.
3. to 5. **Matroska, signature table, advisory puremagic.** Not implemented
   yet (#393 PR B). Until then a file rule 2 does not claim is decided as
   before #393: by the first ``puremagic`` match with an image, video or
   audio MIME type. The one change is that a match shorter than
   :data:`MIN_PUREMAGIC_MATCH_BYTES` with MIME type ``audio/x-sndr`` (the
   two-byte ``00 00`` match ``puremagic`` calls ``.sndr``) never decides.
6. **Extension.** The registry's name lookup, used when no rule above
   recognised the content.

Only regular files are read. A FIFO, socket or device is opened
non-blocking, or not at all, and gets the extension answer with a signal
saying its content was not read; ``detect`` never blocks on it.

A container that rule 2 recognises but cannot resolve, finds malformed, or
cannot finish within its budget is unknown (``media_type=None``) with a
signal that says why. The extension never replaces that answer.

Rule 2 reads at most :data:`MAX_BYTES_READ` bytes, the first bytes it reads
to decide whether to claim the file included. ``puremagic`` opens and reads
the file separately and outside that budget: ``puremagic.magic_file`` reads
the first ``puremagic.main.max_head`` bytes and the last
``puremagic.main.max_foot`` bytes (36,870 and 516 in puremagic 2.3.0), and
its deep scanners may reopen the file and read more, the text scanner up to
1 MB.

Every result lists its evidence in :attr:`DetectionResult.signals`. The
signal that carries the decision has a ``detail`` starting with
``decision=``; the text after it names the rule and the reason.
"""

from __future__ import annotations

import os
import stat
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final, Literal

import puremagic
from pydantic import BaseModel, ConfigDict

from winnow.media import registry as _registry
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

MIN_PUREMAGIC_MATCH_BYTES: Final[int] = 3
"""Shortest ``audio/x-sndr`` ``puremagic`` byte match that may decide."""

_BOX_HEADER: Final[int] = 8
_LARGE_BOX_HEADER: Final[int] = 16
_FTYP_FIXED_PAYLOAD: Final[int] = 8
_BRAND_BYTES: Final[int] = 4
_HANDLER_PAYLOAD: Final[int] = 12
_USERTYPE_BYTES: Final[int] = 16

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

_SIZED_OPENING_TYPES: Final[frozenset[bytes]] = frozenset(
    {_MOOV, b"mdat", b"free", b"skip", b"wide", b"pnot"},
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

_SNDR_MIME: Final[str] = "audio/x-sndr"
_HEAD_BYTES: Final[int] = _LARGE_BOX_HEADER
_NOT_REGULAR: Final[str] = "unavailable=content not read: not a regular file"

_MIME_PREFIX_TYPES: Final[dict[str, MediaType]] = {
    "audio": MediaType.AUDIO,
    "image": MediaType.IMAGE,
    "video": MediaType.VIDEO,
}

SignalSource = Literal[
    "override",
    "ftyp",
    "track-handler",
    "signature",
    "puremagic",
    "extension",
]


class TypeSignal(BaseModel):
    """One piece of evidence about a file's media type.

    Args:
        source: Where the evidence came from.
        detail: What was seen, for example ``"major_brand=heic"``,
            ``"hdlr=soun"`` or ``".mp4 video/mp4 0.8"``. The signal that
            carries the decision starts with ``"decision="``.
        media_type: Media type this evidence points to, if any.
    """

    model_config = ConfigDict(frozen=True)

    source: SignalSource
    detail: str
    media_type: MediaType | None


class DetectionResult(BaseModel):
    """Outcome of :func:`detect` with the evidence behind it.

    Args:
        media_type: Decided media type, or ``None`` when not established.
        container: Container recognised from the content, such as
            ``"iso-bmff"`` or ``"quicktime"``.
        major_brand: ISO-BMFF ``ftyp`` major brand, spaces preserved.
        compatible_brands: ISO-BMFF compatible brands in file order.
        signals: Evidence gathered, the deciding signal included.
        extension_media_type: What the extension alone would say.
        agrees_with_extension: Whether ``media_type`` equals
            ``extension_media_type``; ``None`` when either is unknown.
    """

    model_config = ConfigDict(frozen=True)

    media_type: MediaType | None
    container: str | None
    major_brand: str | None
    compatible_brands: tuple[str, ...] = ()
    signals: tuple[TypeSignal, ...]
    extension_media_type: MediaType | None
    agrees_with_extension: bool | None


class _MalformedError(Exception):
    """The box structure breaks a rule of ISO/IEC 14496-12."""


class _BudgetExceededError(Exception):
    """A valid structure needs more than the walk may read or visit."""


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
class _Reader:
    """Read a file at offsets while counting bytes and boxes against caps.

    Args:
        handle: Open binary file.
        size: File size in bytes.
    """

    handle: BinaryIO
    size: int
    bytes_read: int = 0
    boxes: int = 0

    def read_at(self, offset: int, length: int) -> bytes:
        """Read up to ``length`` bytes at ``offset``.

        Args:
            offset: File offset.
            length: Bytes wanted.

        Returns:
            The bytes read; shorter only at the end of the file.

        Raises:
            _BudgetExceededError: If the read would pass ``MAX_BYTES_READ``.
        """
        if self.bytes_read + length > MAX_BYTES_READ:
            raise _BudgetExceededError("read budget")
        self.handle.seek(offset)
        data = self.handle.read(length)
        self.bytes_read += len(data)
        return data

    def read_exact(self, offset: int, length: int, what: str) -> bytes:
        """Read exactly ``length`` bytes at ``offset``.

        Args:
            offset: File offset.
            length: Bytes needed.
            what: Name of the structure, for the error message.

        Returns:
            The bytes read.

        Raises:
            _MalformedError: If the file ends first.
        """
        data = self.read_at(offset, length)
        if len(data) != length:
            raise _MalformedError(f"truncated {what}")
        return data


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


@dataclass(frozen=True, slots=True)
class _Outcome:
    """A rule's answer.

    Args:
        media_type: Decided media type, possibly ``None`` (unknown).
        signals: Evidence, the deciding signal last.
        container: Recognised container, if any.
        major_brand: ``ftyp`` major brand, if any.
        compatible_brands: ``ftyp`` compatible brands.
    """

    media_type: MediaType | None
    signals: tuple[TypeSignal, ...]
    container: str | None = None
    major_brand: str | None = None
    compatible_brands: tuple[str, ...] = ()


def detect(
    path: Path,
    *,
    registry: _registry.FormatRegistry | None = None,
) -> DetectionResult:
    """Detect a file's media type and record the evidence.

    Args:
        path: File to classify. It need not exist, nor be a regular file;
            then only the extension can decide.
        registry: Registry supplying overrides and the extension lookup.
            Defaults to the shared default registry.

    Returns:
        The decision with its signals.
    """
    active = registry if registry is not None else _registry.DEFAULT_FORMAT_REGISTRY
    extension_type = active.lookup(path.suffix)
    extension_signal = TypeSignal(
        source="extension",
        detail=f"suffix={path.suffix.casefold() or '(none)'}",
        media_type=extension_type,
    )
    try:
        handle = _open_regular(path)
    except OSError as exc:
        unavailable = TypeSignal(
            source="signature",
            detail=_unavailable_detail(exc),
            media_type=None,
        )
        outcome = _override(active, path) or _by_extension(extension_type, ())
        return _result(
            outcome,
            advisory=(unavailable, extension_signal),
            extension_type=extension_type,
        )
    with handle:
        matches = _puremagic_matches(path)
        advisory = (*_puremagic_signals(matches), extension_signal)
        outcome = _override(active, path) or _inspect_content(
            handle=handle,
            matches=matches,
            extension_type=extension_type,
        )
    return _result(outcome, advisory=advisory, extension_type=extension_type)


def _result(
    outcome: _Outcome,
    *,
    advisory: tuple[TypeSignal, ...],
    extension_type: MediaType | None,
) -> DetectionResult:
    """Build the public result from a rule's outcome.

    Args:
        outcome: The deciding rule's outcome.
        advisory: Evidence recorded after the decision.
        extension_type: Media type the extension gives.

    Returns:
        The result.
    """
    media_type = outcome.media_type
    agrees = (
        None
        if media_type is None or extension_type is None
        else media_type == extension_type
    )
    return DetectionResult(
        media_type=media_type,
        container=outcome.container,
        major_brand=outcome.major_brand,
        compatible_brands=outcome.compatible_brands,
        signals=(*outcome.signals, *advisory),
        extension_media_type=extension_type,
        agrees_with_extension=agrees,
    )


def _override(active: _registry.FormatRegistry, path: Path) -> _Outcome | None:
    """Apply rule 1, the user override.

    Args:
        active: Registry in use.
        path: File being classified.

    Returns:
        The override's outcome, or ``None`` without one.
    """
    override = active.lookup_override(path.suffix)
    if override is None:
        return None
    signal = TypeSignal(
        source="override",
        detail=f"decision=override suffix={path.suffix.casefold()}",
        media_type=override,
    )
    return _Outcome(media_type=override, signals=(signal,))


class _NotRegularFileError(OSError):
    """The path names a FIFO, socket, device or directory, not a file."""


def _open_regular(path: Path) -> BinaryIO:
    """Open ``path`` for reading only if it is a regular file.

    The path is checked before opening, so a socket is never opened, and the
    descriptor is checked after opening, so a file swapped for a FIFO in
    between is caught. The open is non-blocking, so a FIFO cannot stall it.

    Args:
        path: File to open.

    Returns:
        The open binary file.

    Raises:
        _NotRegularFileError: If ``path`` is not a regular file.
    """
    if not stat.S_ISREG(path.stat().st_mode):
        raise _NotRegularFileError(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _NotRegularFileError(path)
    except OSError:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "rb")


def _unavailable_detail(exc: OSError) -> str:
    """Describe why the content could not be read.

    Args:
        exc: The error from opening or reading the file.

    Returns:
        The signal detail.
    """
    if isinstance(exc, _NotRegularFileError):
        return _NOT_REGULAR
    return f"unavailable=content {type(exc).__name__}"


def _inspect_content(
    *,
    handle: BinaryIO,
    matches: list[puremagic.PureMagicWithConfidence],
    extension_type: MediaType | None,
) -> _Outcome:
    """Apply rules 2 to 6 to the file's content.

    Args:
        handle: The open regular file.
        matches: ``puremagic`` matches, possibly empty.
        extension_type: Media type the extension gives.

    Returns:
        The deciding rule's outcome.
    """
    try:
        size = handle.seek(0, os.SEEK_END)
        bmff = _inspect_bmff(_Reader(handle=handle, size=size))
    except OSError as exc:
        unavailable = TypeSignal(
            source="signature",
            detail=_unavailable_detail(exc),
            media_type=None,
        )
        return _by_extension(extension_type, (unavailable,))
    if bmff is not None:
        return bmff
    return _first_puremagic_match(matches) or _by_extension(extension_type, ())


def _by_extension(
    extension_type: MediaType | None,
    signals: tuple[TypeSignal, ...],
) -> _Outcome:
    """Apply rule 6, the extension fallback.

    Args:
        extension_type: Media type the extension gives.
        signals: Signals gathered before falling back.

    Returns:
        The extension's outcome.
    """
    decision = TypeSignal(
        source="extension",
        detail="decision=extension fallback",
        media_type=extension_type,
    )
    return _Outcome(media_type=extension_type, signals=(*signals, decision))


# --- rule 2: ISO-BMFF and QuickTime -----------------------------------------


def _inspect_bmff(reader: _Reader) -> _Outcome | None:
    """Apply rule 2 when the file opens with an ISO-BMFF or QuickTime box.

    The opening bytes are read through ``reader``, so they count against
    :data:`MAX_BYTES_READ`.

    Args:
        reader: Reader over the open file.

    Returns:
        The rule's outcome, or ``None`` when rule 2 does not claim the file.
    """
    state = _BmffState()
    try:
        head = reader.read_at(0, _HEAD_BYTES)
        if not _claims(head, reader.size):
            return None
        _walk_top_level(reader, state)
    except _MalformedError as exc:
        return _bmff_outcome(state, None, f"unknown malformed: {exc}")
    except _BudgetExceededError as exc:
        return _bmff_outcome(state, None, f"unknown budget-exceeded: {exc}")
    media_type, reason = _classify_bmff(state)
    return _bmff_outcome(state, media_type, reason)


def _claims(head: bytes, size: int) -> bool:
    """Decide from the first box header whether rule 2 claims the file.

    A first box of type ``ftyp`` is always claimed. A first box of another
    opening type is claimed only when its declared size is plausible, so
    text that happens to hold ``free`` at offset 4 is not QuickTime.

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
        return True
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


def _walk_top_level(reader: _Reader, state: _BmffState) -> None:
    """Visit top-level boxes until the brands and tracks are known.

    Payloads are skipped by seeking, so ``moov`` after a large ``mdat`` is
    found without reading the ``mdat``.

    Args:
        reader: Reader over the open file.
        state: Walk state to fill in.
    """
    boxes = _iter_boxes(
        reader,
        start=0,
        end=reader.size,
        depth=0,
        on_kind=lambda kind: _note_top_level(state, kind),
    )
    for box in boxes:
        if box.kind == _FTYP and not state.has_ftyp:
            _read_ftyp(reader, box, state)
        elif box.kind == _MOOV and not state.has_moov:
            state.has_moov = True
            _walk_children(reader, box, state, path=(_MOOV,))
        if _walk_done(state):
            return


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
        ``True`` once ``moov`` is read, or once ``ftyp`` alone decides an
        image. A ``moov`` read before any ``ftyp`` is legacy QuickTime: no
        later ``ftyp`` is looked for, so trailing boxes cannot spoil it.
    """
    if state.has_moov:
        return True
    brands = state.brands
    return (
        state.has_ftyp
        and CANON_CRX_BRAND not in brands
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
    reader: _Reader,
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
        _BudgetExceededError: Past ``MAX_BOX_DEPTH`` or ``MAX_BOXES``.
        _MalformedError: On a truncated header, a size smaller than its
            header, or a box past its parent or the file.
    """
    if depth > MAX_BOX_DEPTH:
        raise _BudgetExceededError("box depth")
    offset = start
    while offset < end:
        reader.boxes += 1
        if reader.boxes > MAX_BOXES:
            raise _BudgetExceededError("box count")
        # A box is never smaller than its header, so each step moves forward.
        box = _read_box_header(reader, offset=offset, end=end, on_kind=on_kind)
        yield box
        offset = box.end


def _read_box_header(
    reader: _Reader,
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
        _MalformedError: On a truncated header, a size smaller than its
            header, or a box past ``end``.
    """
    if end - offset < _BOX_HEADER:
        raise _MalformedError("truncated box header")
    raw = reader.read_exact(offset, _BOX_HEADER, "box header")
    (size,) = struct.unpack(">I", raw[:4])
    kind = raw[4:8]
    if on_kind is not None:
        on_kind(kind)
    header = _BOX_HEADER
    if size == 1:
        if end - offset < _LARGE_BOX_HEADER:
            raise _MalformedError("truncated extended box header")
        large = reader.read_exact(offset + _BOX_HEADER, 8, "extended box size")
        (size,) = struct.unpack(">Q", large)
        header = _LARGE_BOX_HEADER
    elif size == 0:
        size = end - offset
    if size < header:
        raise _MalformedError("non-progressing box: size smaller than its header")
    if offset + size > end:
        where = "the file" if end == reader.size else "its parent"
        raise _MalformedError(f"box extends past {where}")
    usertype = None
    if kind == _UUID:
        if size < header + _USERTYPE_BYTES:
            raise _MalformedError("uuid box shorter than its usertype")
        usertype = reader.read_exact(offset + header, _USERTYPE_BYTES, "usertype")
        header += _USERTYPE_BYTES
    return _Box(
        kind=kind,
        offset=offset,
        payload=offset + header,
        end=offset + size,
        usertype=usertype,
    )


def _read_ftyp(reader: _Reader, box: _Box, state: _BmffState) -> None:
    """Read the brands of an ``ftyp`` box.

    Args:
        reader: Reader over the open file.
        box: The ``ftyp`` box.
        state: Walk state to fill in.

    Raises:
        _MalformedError: If the payload is shorter than major brand and
            minor version, or its brand list is not whole brands.
        _BudgetExceededError: If the payload is over
            ``MAX_FTYP_PAYLOAD_BYTES``.
    """
    length = box.end - box.payload
    if length < _FTYP_FIXED_PAYLOAD:
        raise _MalformedError("ftyp shorter than its minimum size")
    if (length - _FTYP_FIXED_PAYLOAD) % _BRAND_BYTES:
        raise _MalformedError("ftyp brand list is not a multiple of four bytes")
    if length > MAX_FTYP_PAYLOAD_BYTES:
        raise _BudgetExceededError("ftyp larger than its read budget")
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
    reader: _Reader,
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


def _read_handler(reader: _Reader, box: _Box) -> str:
    """Read the handler type of an ``hdlr`` box.

    Args:
        reader: Reader over the open file.
        box: The ``hdlr`` box.

    Returns:
        The four-character handler type, such as ``"vide"``.

    Raises:
        _MalformedError: If the payload is too short to hold it.
    """
    if box.end - box.payload < _HANDLER_PAYLOAD:
        raise _MalformedError("hdlr shorter than its handler type")
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
        return None, "unknown unresolved: no moov"
    if _VIDEO_HANDLER in state.handlers:
        return MediaType.VIDEO, "track handler vide"
    if _SOUND_HANDLER in state.handlers:
        return MediaType.AUDIO, "track handlers soun without vide"
    return None, "unknown unresolved: no vide or soun track"


def _bmff_outcome(
    state: _BmffState,
    media_type: MediaType | None,
    reason: str,
) -> _Outcome:
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
    container = "iso-bmff" if state.saw_ftyp else "quicktime"
    signals.append(
        TypeSignal(
            source="ftyp" if state.saw_ftyp else "track-handler",
            detail=f"decision={container} {reason}",
            media_type=media_type,
        ),
    )
    return _Outcome(
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


# --- rules 3 to 5: signatures and puremagic ---------------------------------


def _puremagic_matches(path: Path) -> list[puremagic.PureMagicWithConfidence]:
    """Return ``puremagic``'s matches for ``path``.

    Args:
        path: File to inspect.

    Returns:
        Matches in ``puremagic``'s order; empty when it fails.
    """
    try:
        return list(puremagic.magic_file(str(path)))
    except (puremagic.PureError, OSError, ValueError):
        return []


def _match_media_type(match: puremagic.PureMagicWithConfidence) -> MediaType | None:
    """Map a ``puremagic`` match to a media type by its MIME prefix.

    Args:
        match: One match.

    Returns:
        Image, video or audio, or ``None`` for anything else.
    """
    mime_prefix, _, _ = (match.mime_type or "").partition("/")
    return _MIME_PREFIX_TYPES.get(mime_prefix)


def _match_length(match: puremagic.PureMagicWithConfidence) -> int:
    """Return how many bytes a ``puremagic`` match compared.

    Args:
        match: One match.

    Returns:
        Length of its byte pattern; 0 when it has none.
    """
    return len(match.byte_match or b"")


def _puremagic_signals(
    matches: list[puremagic.PureMagicWithConfidence],
) -> tuple[TypeSignal, ...]:
    """Record every ``puremagic`` match as an advisory signal.

    Args:
        matches: ``puremagic`` matches.

    Returns:
        One signal per match.
    """
    return tuple(
        TypeSignal(
            source="puremagic",
            detail=(
                f"{match.extension} {match.mime_type} {match.confidence}"
                f" bytes={_match_length(match)}"
            ),
            media_type=_match_media_type(match),
        )
        for match in matches
    )


def _decides(match: puremagic.PureMagicWithConfidence) -> bool:
    """Report whether a ``puremagic`` match may decide, as before #393.

    The one exclusion is a match shorter than
    :data:`MIN_PUREMAGIC_MATCH_BYTES` whose MIME type is ``audio/x-sndr``.

    Args:
        match: One match.

    Returns:
        ``True`` unless the match is a short ``audio/x-sndr`` match.
    """
    return not (
        match.mime_type == _SNDR_MIME
        and _match_length(match) < MIN_PUREMAGIC_MATCH_BYTES
    )


def _first_puremagic_match(
    matches: list[puremagic.PureMagicWithConfidence],
) -> _Outcome | None:
    """Stand in for rules 3 to 5 until #393 PR B.

    Takes the first ``puremagic`` match with an image, video or audio MIME
    type, as ``main`` did before #393, skipping short ``audio/x-sndr``
    matches.

    Args:
        matches: ``puremagic`` matches in its order.

    Returns:
        The outcome, or ``None`` when no match may decide.
    """
    for match in matches:
        media_type = _match_media_type(match)
        if media_type is None or not _decides(match):
            continue
        decision = TypeSignal(
            source="puremagic",
            detail=(
                f"decision=puremagic first media match {match.extension}"
                f" {match.mime_type} (rules 3-5 pending, #393 PR B)"
            ),
            media_type=media_type,
        )
        return _Outcome(media_type=media_type, signals=(decision,))
    return None


__all__ = [
    "AUDIO_BRANDS",
    "AVIF_IMAGE_BRANDS",
    "CANON_CRX_BRAND",
    "CANON_PROFILE_BYTES",
    "CANON_UUID",
    "HEIF_IMAGE_BRANDS",
    "IMAGE_SEQUENCE_BRANDS",
    "MAX_BOXES",
    "MAX_BOX_DEPTH",
    "MAX_BYTES_READ",
    "MAX_FTYP_PAYLOAD_BYTES",
    "MIN_PUREMAGIC_MATCH_BYTES",
    "STRUCTURAL_IMAGE_BRANDS",
    "TRACK_BOX",
    "DetectionResult",
    "TypeSignal",
    "detect",
]
