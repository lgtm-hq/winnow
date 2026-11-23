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
    every ``id`` with :func:`~winnow.models.evidence.observation_id`.
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

    A file that cannot be stat'ed, is not a regular file or cannot be opened
    gives one ``unreadable`` issue and no observations: nothing, not even its
    name, is read as evidence. A reader that raises gives one ``failed``
    issue for that reader only. A file whose size, ``mtime_ns``,
    ``ctime_ns``, device or inode differs after reading, or that vanished,
    gives a ``file-changed`` issue. Equal stats are a freshness hint, never
    proof the content is unchanged.

    Args:
        path: The file.
        readers: Readers to run, in order.
        registry: Registry for detection; the default registry when
            ``None``. Ignored when ``detection`` is given.
        detection: The file's detection result when the caller already has
            one; detection then does not read the file again.

    Returns:
        The evidence, every observation carrying its stable ``id``. IDs use
        the file's own snapshot; sidecar snapshots arrive with #402.
    """
    try:
        before = _snapshot(path)
    except OSError as exc:
        return _unreadable(path, snapshot=None, exc=exc)
    try:
        _check_readable(path)
    except OSError as exc:
        return _unreadable(path, snapshot=before, exc=exc)

    if detection is None:
        detection = detect(path, registry=registry)
    observations = _type_observations(path, detection)
    issues: list[ReadIssue] = []
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
    """
    result = os.stat(path)
    birthtime: int | None = getattr(result, "st_birthtime_ns", None)
    return FileSnapshot(
        path=path,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
        device=result.st_dev,
        inode=result.st_ino,
        ctime_ns=result.st_ctime_ns,
        birthtime_ns=birthtime,
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
            raise _NotRegularFileError(f"not a regular file: {path}")
    finally:
        os.close(descriptor)


def _unreadable(
    path: Path,
    *,
    snapshot: FileSnapshot | None,
    exc: OSError,
) -> EvidenceSet:
    """Build the result for a file that cannot be read.

    Args:
        path: The file.
        snapshot: Its snapshot, if it could be stat'ed.
        exc: Why it cannot be read.

    Returns:
        An evidence set with one ``unreadable`` issue and no observations.
    """
    issue = ReadIssue(
        reader=COLLECTOR,
        status="unreadable",
        message=f"{path}: {type(exc).__name__}: {exc.strerror or exc}",
    )
    return EvidenceSet(snapshot=snapshot, observations=(), issues=(issue,))


def _run_reader(
    reader: EvidenceReader,
    path: Path,
    detection: DetectionResult,
) -> tuple[list[Observation], list[ReadIssue]]:
    """Run one reader, turning an exception into a ``failed`` issue.

    Args:
        reader: The reader.
        path: The file.
        detection: The file's detection result.

    Returns:
        The reader's observations and issues; on an exception, no
        observations and one ``failed`` issue.
    """
    try:
        return reader.read(path, detection=detection)
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
    except OSError as exc:
        return ReadIssue(
            reader=COLLECTOR,
            status="file-changed",
            message=f"vanished during read: {type(exc).__name__}",
        )
    fields = ("size", "mtime_ns", "ctime_ns", "device", "inode")
    differing = [
        name for name in fields if getattr(before, name) != getattr(after, name)
    ]
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
    signal. The container, the ``ftyp`` major brand and each compatible
    brand become ``container`` observations when detection found them.

    Args:
        path: The file.
        detection: Its detection result.

    Returns:
        The observations, ids not yet assigned.
    """
    observations = [
        _signal_observation(path, index=index, signal=signal)
        for index, signal in enumerate(detection.signals)
    ]
    if detection.container is not None:
        observations.append(
            _container_observation(
                path,
                source=_deciding_source(detection),
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


def _signal_observation(path: Path, *, index: int, signal: TypeSignal) -> Observation:
    """Turn one detection signal into a ``media_type`` observation.

    Args:
        path: The file.
        index: The signal's position in the detection result, which makes
            the locator of each occurrence distinct.
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
        locator=f"detect:signals[{index}]",
        origin=path,
        raw=signal.detail,
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


def _deciding_source(detection: DetectionResult) -> SourceKind:
    """Return the source of the signal that carried the decision.

    Args:
        detection: The detection result.

    Returns:
        The deciding signal's source; ``signature`` if none is marked.
    """
    for signal in detection.signals:
        if signal.detail.startswith(_DECISION_PREFIX):
            return _SIGNAL_SOURCES[signal.source]
    return SourceKind.SIGNATURE


__all__ = [
    "COLLECTOR",
    "EvidenceReader",
    "collect_evidence",
]
