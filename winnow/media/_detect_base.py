"""Shared types for media-type detection (#393).

The public models (:class:`TypeSignal`, :class:`DetectionResult`) and the
internal outcome and reader types every detection rule uses. The rules live
in :mod:`winnow.media._detect` (order and fallbacks),
:mod:`winnow.media._detect_bmff` (rule 2), :mod:`winnow.media._matroska`
(rule 3), :mod:`winnow.media._signatures` (rule 4) and the container walkers
those modules call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO, Final, Literal

from pydantic import BaseModel, ConfigDict

from winnow.models.media import MediaType

SignalSource = Literal[
    "override",
    "ftyp",
    "track-handler",
    "signature",
    "puremagic",
    "extension",
]

DECISION_PREFIX: Final[str] = "decision="
"""Prefix of the ``detail`` of the one signal that carries the decision."""


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
    """Outcome of :func:`winnow.media._detect.detect` with its evidence.

    Args:
        media_type: Decided media type, or ``None`` when not established.
        container: Container or format recognised from the content, such as
            ``"iso-bmff"``, ``"matroska"``, ``"jpeg"`` or ``"pdf"``.
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


@dataclass(frozen=True, slots=True)
class Outcome:
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


def decide(
    container: str,
    media_type: MediaType | None,
    reason: str,
    *,
    evidence: tuple[TypeSignal, ...] = (),
    source: SignalSource = "signature",
) -> Outcome:
    """Build a decided outcome for a recognised container.

    Args:
        container: Container or format name, also the start of the detail.
        media_type: Decided media type, ``None`` for unknown or non-media.
        reason: Why, appended to the detail.
        evidence: Signals gathered before deciding.
        source: Source of the deciding signal.

    Returns:
        The outcome, its deciding signal last.
    """
    decision = TypeSignal(
        source=source,
        detail=f"{DECISION_PREFIX}{container} {reason}",
        media_type=media_type,
    )
    return Outcome(
        media_type=media_type,
        signals=(*evidence, decision),
        container=container,
    )


def evidence(
    detail: str,
    media_type: MediaType | None = None,
    *,
    source: SignalSource = "signature",
) -> TypeSignal:
    """Build a non-deciding signal.

    Args:
        detail: What was seen.
        media_type: Media type it points to, if any.
        source: Where it came from.

    Returns:
        The signal.
    """
    return TypeSignal(source=source, detail=detail, media_type=media_type)


class MalformedError(Exception):
    """The structure breaks a rule of its format's specification."""


class BudgetExceededError(Exception):
    """A valid structure needs more than detection may read or visit."""


@dataclass(slots=True)
class Reader:
    """Read a file at offsets while counting bytes against a cap.

    Args:
        handle: Open binary file.
        size: File size in bytes.
        budget: Bytes this reader may read in total.
    """

    handle: BinaryIO
    size: int
    budget: int
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
            BudgetExceededError: If the read would pass ``budget``.
        """
        if self.bytes_read + length > self.budget:
            raise BudgetExceededError("read budget")
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
            MalformedError: If the file ends first.
        """
        data = self.read_at(offset, length)
        if len(data) != length:
            raise MalformedError(f"truncated {what}")
        return data


@dataclass(slots=True)
class Content:
    """The bytes rules 3 and 4 inspect, read once from the open file.

    Args:
        head: The file's first bytes, at most the signature head size.
        tail: The file's last bytes, for trailer signatures.
        size: File size in bytes.
        reader: Reader for structures past ``head`` (an ID3v2 tag's end).
        descriptor: Open descriptor handed to ffprobe for stream types.
        hints: Evidence a rule found without deciding.
    """

    head: bytes
    tail: bytes
    size: int
    reader: Reader
    descriptor: int
    hints: list[TypeSignal] = field(default_factory=list)


__all__ = [
    "DECISION_PREFIX",
    "BudgetExceededError",
    "Content",
    "DetectionResult",
    "MalformedError",
    "Outcome",
    "Reader",
    "SignalSource",
    "TypeSignal",
    "decide",
    "evidence",
]
