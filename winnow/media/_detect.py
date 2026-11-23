"""Content-based media-type detection with a structured, explainable result.

:func:`detect` applies these rules in order; the first that decides wins
(#393):

1. **User override.** An extension the user registered on the
   :class:`~winnow.media.registry.FormatRegistry` decides.
2. **ISO-BMFF and QuickTime** (:mod:`winnow.media._detect_bmff`): brands,
   track handlers and Canon profiles from a bounded box walk.
3. **Matroska and WebM** (:mod:`winnow.media._matroska`): a validated EBML
   header, then the ``TrackType`` of every track read natively
   (:mod:`winnow.media._matroska_tracks`). Only when that walk cannot
   settle the type is ffprobe asked; without it, unknown.
4. **Signature table** (:mod:`winnow.media._signatures`): winnow's own
   table of the formats it recognises, non-media formats (PDF, ZIP)
   included, then MPEG transport streams (:mod:`winnow.media._mpegts`) and
   frame-based audio.
5. **puremagic, advisory.** Every ``puremagic`` match is recorded as a
   signal. A match decides only when its media type equals the extension's
   and its byte pattern is at least :data:`MIN_PUREMAGIC_MATCH_BYTES` long.
6. **Extension.** The registry's name lookup, used when no rule above
   recognised the content. Content recognised as non-media (a signature in
   the table, or plain text) is not overridden by the extension.

Only regular files are read. A FIFO, socket or device is opened
non-blocking, or not at all, and gets the extension answer with a signal
saying its content was not read; ``detect`` never blocks on it. The file is
opened once; ``puremagic`` and ffprobe read that open file, never the path
again.

A container a rule recognises but cannot resolve, finds malformed, or
cannot finish within its budget is unknown (``media_type=None``) with a
signal that says why. The extension never replaces that answer.

Rule 2 reads at most :data:`winnow.media._detect_bmff.MAX_BYTES_READ`
bytes; rules 3 and 4 at most
:data:`winnow.media._signatures.MAX_BYTES_READ`. ``puremagic`` reads the
first ``puremagic.main.max_head`` and last ``puremagic.main.max_foot``
bytes of the open file (36,870 and 516 in puremagic 2.3.0), outside those
budgets.

Every result lists its evidence in :attr:`DetectionResult.signals`. The
signal that carries the decision has a ``detail`` starting with
``decision=``; the text after it names the rule and the reason.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO, Final

import puremagic

from winnow.media import registry as _registry
from winnow.media._detect_base import (
    DECISION_PREFIX,
    Content,
    DetectionResult,
    Outcome,
    Reader,
    SignalSource,
    TypeSignal,
    decide,
    evidence,
)
from winnow.media._detect_bmff import (
    AUDIO_BRANDS,
    AVIF_IMAGE_BRANDS,
    CANON_CRX_BRAND,
    CANON_PROFILE_BYTES,
    CANON_UUID,
    HEIF_IMAGE_BRANDS,
    IMAGE_SEQUENCE_BRANDS,
    MAX_BOX_DEPTH,
    MAX_BOXES,
    MAX_BYTES_READ,
    MAX_FTYP_PAYLOAD_BYTES,
    STRUCTURAL_IMAGE_BRANDS,
    TRACK_BOX,
    inspect_bmff,
)
from winnow.media._header_checks import is_text
from winnow.media._matroska import inspect_matroska
from winnow.media._signatures import HEAD_BYTES, TAIL_BYTES, match_signature
from winnow.media._signatures import MAX_BYTES_READ as SIGNATURE_BYTES_READ
from winnow.models.media import MediaType

MIN_PUREMAGIC_MATCH_BYTES: Final[int] = 3
"""Shortest ``puremagic`` byte match that may decide."""

NOT_REGULAR: Final[str] = "unavailable=content not read: not a regular file"
"""Signal detail for a path whose content was not read."""

_MIME_PREFIX_TYPES: Final[dict[str, MediaType]] = {
    "audio": MediaType.AUDIO,
    "image": MediaType.IMAGE,
    "video": MediaType.VIDEO,
}


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
        unavailable = evidence(_unavailable_detail(exc))
        outcome = _override(active, path) or _by_extension(extension_type, ())
        return _result(
            outcome,
            advisory=(unavailable, extension_signal),
            extension_type=extension_type,
        )
    with handle:
        matches = _puremagic_matches(handle)
        advisory = (*_puremagic_signals(matches), extension_signal)
        outcome = _override(active, path) or _inspect_content(
            handle=handle,
            matches=matches,
            extension_type=extension_type,
        )
    return _result(outcome, advisory=advisory, extension_type=extension_type)


def _result(
    outcome: Outcome,
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


def _override(active: _registry.FormatRegistry, path: Path) -> Outcome | None:
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
        detail=f"{DECISION_PREFIX}override suffix={path.suffix.casefold()}",
        media_type=override,
    )
    return Outcome(media_type=override, signals=(signal,))


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
        return NOT_REGULAR
    return f"unavailable=content {type(exc).__name__}"


def _inspect_content(
    *,
    handle: BinaryIO,
    matches: list[puremagic.PureMagicWithConfidence],
    extension_type: MediaType | None,
) -> Outcome:
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
        bmff = inspect_bmff(handle, size)
        if bmff is not None:
            return bmff
        content = _read_content(handle, size)
        decided = inspect_matroska(content) or match_signature(content)
    except OSError as exc:
        return _by_extension(extension_type, (evidence(_unavailable_detail(exc)),))
    if decided is not None:
        return decided
    hints = tuple(content.hints)
    agreeing = _agreeing_puremagic_match(matches, extension_type, hints)
    if agreeing is not None:
        return agreeing
    if is_text(content.head, size=size):
        return decide("text", None, "non-media plain text", evidence=hints)
    return _by_extension(extension_type, hints)


def _read_content(handle: BinaryIO, size: int) -> Content:
    """Read the bytes rules 3 and 4 inspect.

    Args:
        handle: The open regular file.
        size: File size in bytes.

    Returns:
        The opening and closing bytes and a reader for the rest.
    """
    reader = Reader(handle=handle, size=size, budget=SIGNATURE_BYTES_READ)
    head = reader.read_at(0, HEAD_BYTES)
    tail_start = max(size - TAIL_BYTES, 0)
    if size <= len(head):
        tail = head[tail_start:]
    else:
        tail = reader.read_at(tail_start, TAIL_BYTES)
    return Content(
        head=head,
        tail=tail,
        size=size,
        reader=reader,
        descriptor=handle.fileno(),
    )


def _by_extension(
    extension_type: MediaType | None,
    signals: tuple[TypeSignal, ...],
) -> Outcome:
    """Apply rule 6, the extension fallback.

    Args:
        extension_type: Media type the extension gives.
        signals: Signals gathered before falling back.

    Returns:
        The extension's outcome.
    """
    decision = TypeSignal(
        source="extension",
        detail=f"{DECISION_PREFIX}extension fallback",
        media_type=extension_type,
    )
    return Outcome(media_type=extension_type, signals=(*signals, decision))


# --- rule 5: puremagic, advisory --------------------------------------------


def _puremagic_matches(handle: BinaryIO) -> list[puremagic.PureMagicWithConfidence]:
    """Return ``puremagic``'s matches for the open file.

    ``puremagic`` reads the open handle; it is given no file name, so it
    never reopens the path and its deep scanners never run.

    Args:
        handle: The open regular file.

    Returns:
        Matches in ``puremagic``'s order; empty when it fails.
    """
    try:
        return list(puremagic.magic_stream(handle))
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
    source: SignalSource = "puremagic"
    return tuple(
        TypeSignal(
            source=source,
            detail=(
                f"{match.extension} {match.mime_type} {match.confidence}"
                f" bytes={_match_length(match)}"
            ),
            media_type=_match_media_type(match),
        )
        for match in matches
    )


def _agreeing_puremagic_match(
    matches: list[puremagic.PureMagicWithConfidence],
    extension_type: MediaType | None,
    hints: tuple[TypeSignal, ...],
) -> Outcome | None:
    """Apply rule 5: a ``puremagic`` match decides only with the extension.

    Args:
        matches: ``puremagic`` matches in its order.
        extension_type: Media type the extension gives.
        hints: Evidence earlier rules recorded without deciding.

    Returns:
        The outcome of the first match whose media type equals the
        extension's and whose byte pattern is long enough, or ``None``.
    """
    if extension_type is None:
        return None
    for match in matches:
        if _match_media_type(match) != extension_type:
            continue
        if _match_length(match) < MIN_PUREMAGIC_MATCH_BYTES:
            continue
        decision = TypeSignal(
            source="puremagic",
            detail=(
                f"{DECISION_PREFIX}puremagic {match.extension} {match.mime_type}"
                " agrees with the extension"
            ),
            media_type=extension_type,
        )
        return Outcome(media_type=extension_type, signals=(*hints, decision))
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
    "NOT_REGULAR",
    "STRUCTURAL_IMAGE_BRANDS",
    "TRACK_BOX",
    "DetectionResult",
    "TypeSignal",
    "detect",
]
