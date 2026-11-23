"""Collect a file's evidence (#394).

:func:`collect_evidence` stats the file, records the content-detection
result as type evidence, runs each :class:`EvidenceReader` in the given
order and stats the file again. It adds no date readers (#395, #402) and
decides nothing (#396).

Observation order is deterministic: the type observations first (one
``media_type`` observation per detection signal in signal order, then the
container, the ``ftyp`` major brand and each compatible brand), then each
reader's observations in reader order and in the order that reader
documents.

Rules:

- **Unreadable.** A file that cannot be stat'ed, is not a regular file or
  cannot be opened, or whose content detection could not read, gives one
  ``unreadable`` issue and no observations. Neither the type adapter nor any
  reader runs, so nothing is taken from the file's name alone.
- **Reused detection.** A given :class:`DetectionResult` is used only when
  its :attr:`~DetectionResult.snapshot` matches the file's snapshot on every
  :data:`~winnow.models.evidence.FRESHNESS_FIELDS` field (path, size,
  ``mtime_ns``, ``ctime_ns``, device, inode). Otherwise, including when it
  has no snapshot, the file is detected afresh. A matching result is never
  detected again. A given result whose content read failed has no snapshot,
  so the file is read again, and a successful second read is accepted: a
  successful read establishes the evidence.
- **Changed.** When detection read a different file state than the
  snapshot, the set holds only a ``file-changed`` issue: no observations,
  no readers run; the caller decides when to collect again. A different
  stat after reading, or a file that vanished, adds a ``file-changed``
  issue to the collected evidence. Equal stats are a freshness hint, never
  proof the content is unchanged.
- **Foreign origins.** An observation whose ``origin`` is not the collected
  path is not stored; its reader gets a ``failed`` issue. Sidecar evidence
  arrives with #402.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

from loguru import logger

from winnow import __version__
from winnow.media import (
    DetectionResult,
    FormatRegistry,
    TypeSignal,
    detect,
)
from winnow.models.evidence import (
    Claim,
    EvidenceSet,
    FileSnapshot,
    Observation,
    ParseStatus,
    ReadIssue,
    SourceKind,
    observation_id,
)

COLLECTOR: Final[str] = "winnow"
"""Reader name of the type observations and of the collector's own issues."""

_DECISION_PREFIX: Final[str] = "decision="

_SIGNAL_SOURCES: Final[dict[str, SourceKind]] = {
    "override": SourceKind.OVERRIDE,
    "ftyp": SourceKind.FTYP,
    "track-handler": SourceKind.TRACK_HANDLER,
    "signature": SourceKind.SIGNATURE,
    "puremagic": SourceKind.PUREMAGIC,
    "extension": SourceKind.EXTENSION,
}
"""Detection signal source to evidence source, one to one."""


class EvidenceReader(Protocol):
    """A reader that turns one file into observations.

    A reader documents the order of the observations it returns. It may
    leave each observation's ``id`` empty: :func:`collect_evidence` assigns
    every ``id`` with :func:`~winnow.models.evidence.observation_id`. Every
    observation's ``origin`` must be the ``path`` it was given.
    """

    name: str

    def version(self) -> str:
        """Return the version of the reader and its backend.

        Returns:
            The version string recorded as ``reader_version``.
        """
        ...

    def read(
        self,
        path: Path,
        *,
        detection: DetectionResult,
    ) -> tuple[list[Observation], list[ReadIssue]]:
        """Read one file.

        Args:
            path: The file.
            detection: The file's content-detection result.

        Returns:
            The observations in the reader's documented order, and the
            reasons some could not be made.
        """
        ...


def collect_evidence(
    path: Path,
    *,
    readers: Sequence[EvidenceReader],
    registry: FormatRegistry | None = None,
    detection: DetectionResult | None = None,
) -> EvidenceSet:
    """Collect every observation about one file.

    See the module docstring for the unreadable, reuse, change and origin
    rules. A given ``detection`` whose content read failed has no snapshot,
    so the file is read again and a successful second read is accepted.
    When detection read a newer file state than the snapshot, the set holds
    only a ``file-changed`` issue and the caller decides when to collect again.

    Args:
        path: The file.
        readers: Readers to run, in order.
        registry: Registry for detection; the default registry when
            ``None``. Unused when a matching ``detection`` is given.
        detection: The file's detection result when the caller already has
            one; used without detecting again when its snapshot matches.

    Returns:
        The evidence, every observation carrying its stable ``id``. IDs use
        the file's own snapshot; sidecar snapshots arrive with #402.
    """
    try:
        before = _snapshot(path)
    except (OSError, ValueError) as exc:
        return _unreadable(snapshot=None, detail=_describe(path, exc))
    try:
        _check_readable(path)
    except OSError as exc:
        return _unreadable(snapshot=before, detail=_describe(path, exc))

    if detection is None or not _matches(detection, before):
        detection = detect(path, registry=registry)
    if detection.snapshot is None:
        return _unreadable(snapshot=before, detail=_content_failure(detection))

    stale = detection.snapshot.differences(before)
    if stale:
        issue = ReadIssue(
            reader=COLLECTOR,
            status="file-changed",
            message=f"changed before detection read it: {', '.join(stale)}",
        )
        return EvidenceSet(snapshot=before, observations=(), issues=(issue,))

    issues: list[ReadIssue] = []
    observations = _type_observations(path, detection)
    for reader in readers:
        found, problems = _run_reader(reader, path, detection)
        observations.extend(found)
        issues.extend(problems)

    changed = _change(path, before)
    if changed is not None:
        issues.append(changed)
    return EvidenceSet(
        snapshot=before,
        observations=tuple(
            observation.model_copy(
                update={"id": observation_id(observation, snapshot=before)},
            )
            for observation in observations
        ),
        issues=tuple(issues),
    )


def _snapshot(path: Path) -> FileSnapshot:
    """Stat a file into a snapshot.

    Args:
        path: The file; symlinks are followed, as detection follows them.

    Returns:
        The snapshot.

    Raises:
        OSError: If the file cannot be stat'ed.
        ValueError: If the path cannot name a file (an embedded NUL).
    """
    return FileSnapshot.from_stat(path, os.stat(path))


def _matches(detection: DetectionResult, snapshot: FileSnapshot) -> bool:
    """Report whether a given detection result describes this file state.

    Args:
        detection: The given result.
        snapshot: The file's snapshot now.

    Returns:
        ``True`` when the result has a snapshot equal to ``snapshot`` on
        every freshness field.
    """
    return detection.snapshot is not None and not detection.snapshot.differences(
        snapshot,
    )


class _NotRegularFileError(OSError):
    """The path names a directory, FIFO, socket or device."""


def _check_readable(path: Path) -> None:
    """Check that ``path`` is a regular file this process can open.

    The open is non-blocking, so a FIFO swapped in cannot stall it.

    Args:
        path: The file.

    Raises:
        OSError: If it is not a regular file or cannot be opened.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _NotRegularFileError("not a regular file")
    finally:
        os.close(descriptor)


def _describe(path: Path, exc: Exception) -> str:
    """Describe why a file cannot be read.

    Args:
        path: The file.
        exc: The error.

    Returns:
        The path, the error type and its message.
    """
    reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
    return f"{path}: {type(exc).__name__}: {reason}"


def _content_failure(detection: DetectionResult) -> str:
    """Describe why detection did not read the content.

    Args:
        detection: A result without a snapshot.

    Returns:
        The details of its ``unavailable=`` signals, or a generic message.
    """
    details = [
        signal.detail
        for signal in detection.signals
        if signal.detail.startswith("unavailable=")
    ]
    return f"content not read: {'; '.join(details) or 'no snapshot'}"


def _unreadable(*, snapshot: FileSnapshot | None, detail: str) -> EvidenceSet:
    """Build the result for a file that cannot be read.

    Args:
        snapshot: Its snapshot, if it could be stat'ed.
        detail: Why it cannot be read.

    Returns:
        An evidence set with one ``unreadable`` issue and no observations.
    """
    issue = ReadIssue(reader=COLLECTOR, status="unreadable", message=detail)
    return EvidenceSet(snapshot=snapshot, observations=(), issues=(issue,))


def _run_reader(
    reader: EvidenceReader,
    path: Path,
    detection: DetectionResult,
) -> tuple[list[Observation], list[ReadIssue]]:
    """Run one reader, turning an exception into a ``failed`` issue.

    Observations about another file are dropped with a ``failed`` issue.

    Args:
        reader: The reader.
        path: The file.
        detection: The file's detection result.

    Returns:
        The reader's own-file observations and its issues; on an exception,
        no observations and one ``failed`` issue.
    """
    try:
        found, problems = reader.read(path, detection=detection)
    except Exception as exc:
        logger.opt(exception=exc).debug(
            "evidence reader {} failed on {}",
            reader.name,
            path,
        )
        issue = ReadIssue(
            reader=reader.name,
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        return [], [issue]
    own = [observation for observation in found if observation.origin == path]
    foreign = len(found) - len(own)
    if foreign:
        problems = [
            *problems,
            ReadIssue(
                reader=reader.name,
                status="failed",
                message=(
                    f"{foreign} observation(s) about another file not stored:"
                    " sidecar evidence is not supported yet (#402)"
                ),
            ),
        ]
    return own, problems


def _change(path: Path, before: FileSnapshot) -> ReadIssue | None:
    """Compare the file's stat after reading with the snapshot before.

    Args:
        path: The file.
        before: Its snapshot before reading.

    Returns:
        A ``file-changed`` issue, or ``None`` when the stats agree.
    """
    try:
        after = _snapshot(path)
    except (OSError, ValueError) as exc:
        return ReadIssue(
            reader=COLLECTOR,
            status="file-changed",
            message=f"vanished during read: {type(exc).__name__}",
        )
    differing = before.differences(after)
    if not differing:
        return None
    return ReadIssue(
        reader=COLLECTOR,
        status="file-changed",
        message=f"changed during read: {', '.join(differing)}",
    )


def _type_observations(path: Path, detection: DetectionResult) -> list[Observation]:
    """Turn a detection result into type observations.

    Each signal becomes one ``media_type`` observation whose raw value is
    the signal's detail, ``decision=`` prefix included for the deciding
    signal. Its locator is ``detect:<source>:<detail>#<n>``: the detail
    (stored form) is the signal's own identity, since detection supplies no
    more precise one, and ``n`` counts earlier signals with the same source
    and detail, so it moves only between true repeats of an identical
    signal. A distinct signal appearing, disappearing or moving elsewhere
    never changes another's locator. The container, the ``ftyp`` major brand and each
    compatible brand (indexed by its position in the ``ftyp`` box) become
    ``container`` observations when detection found them; a container with
    no deciding signal to attribute it to is not recorded.

    Args:
        path: The file.
        detection: Its detection result.

    Returns:
        The observations, ids not yet assigned.
    """
    seen: dict[tuple[str, str], int] = {}
    observations: list[Observation] = []
    for signal in detection.signals:
        detail = _stored_detail(signal.detail)
        key = (signal.source, detail)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        locator = f"detect:{signal.source}:{detail}#{occurrence}"
        observations.append(_signal_observation(path, locator=locator, signal=signal))
    deciding = _deciding_source(detection)
    if detection.container is not None and deciding is not None:
        observations.append(
            _container_observation(
                path,
                source=deciding,
                locator="detect:container",
                value=detection.container,
            ),
        )
    if detection.major_brand is not None:
        observations.append(
            _container_observation(
                path,
                source=SourceKind.FTYP,
                locator="ftyp:major_brand",
                value=detection.major_brand,
            ),
        )
    observations.extend(
        _container_observation(
            path,
            source=SourceKind.FTYP,
            locator=f"ftyp:compatible_brands[{index}]",
            value=brand,
        )
        for index, brand in enumerate(detection.compatible_brands)
    )
    return observations


def _stored_detail(detail: str) -> str:
    """Return a signal's detail in a form that can be stored exactly.

    Detection copies the file's extension into the detail, and on Linux an
    extension may hold bytes that are not UTF-8, which Python carries as lone
    surrogates. Such a detail is written as ``hex:`` followed by its bytes, as
    file names are; every other detail is returned unchanged. No detail starts
    with ``hex:`` on its own: each starts with a field name or a dot.

    Args:
        detail: The signal's detail as detection produced it.

    Returns:
        The detail, or its bytes in ``hex:`` form when it is not valid Unicode.
    """
    try:
        detail.encode("utf-8")
    except UnicodeEncodeError:
        return "hex:" + os.fsencode(detail).hex()
    return detail


def _signal_observation(
    path: Path,
    *,
    locator: str,
    signal: TypeSignal,
) -> Observation:
    """Turn one detection signal into a ``media_type`` observation.

    Args:
        path: The file.
        locator: The signal's stable locator.
        signal: The signal.

    Returns:
        The observation: ``ok`` with the media type, or ``empty`` when the
        signal names none.
    """
    media_type = signal.media_type
    source = _SIGNAL_SOURCES[signal.source]
    return Observation(
        id="",
        claim=Claim.MEDIA_TYPE,
        source=source,
        locator=locator,
        origin=path,
        raw=_stored_detail(signal.detail),
        value=None if media_type is None else media_type.value,
        status=ParseStatus.EMPTY if media_type is None else ParseStatus.OK,
        family=str(source),
        reader=COLLECTOR,
        reader_version=__version__,
    )


def _container_observation(
    path: Path,
    *,
    source: SourceKind,
    locator: str,
    value: str,
) -> Observation:
    """Build one ``container`` observation.

    Args:
        path: The file.
        source: Where the value was read.
        locator: The exact field.
        value: The container or brand, spaces preserved.

    Returns:
        The observation.
    """
    return Observation(
        id="",
        claim=Claim.CONTAINER,
        source=source,
        locator=locator,
        origin=path,
        raw=value,
        value=value,
        status=ParseStatus.OK,
        family=str(source),
        reader=COLLECTOR,
        reader_version=__version__,
    )


def _deciding_source(detection: DetectionResult) -> SourceKind | None:
    """Return the source of the signal that carried the decision.

    Args:
        detection: The detection result.

    Returns:
        The deciding signal's source, or ``None`` if none is marked.
    """
    for signal in detection.signals:
        if signal.detail.startswith(_DECISION_PREFIX):
            return _SIGNAL_SOURCES[signal.source]
    return None


__all__ = [
    "COLLECTOR",
    "EvidenceReader",
    "collect_evidence",
]
