"""Directory inventory and per-file inspection.

Files are identified with :func:`winnow.media.detect`, the rule Discovery
uses, and enumerated with :func:`winnow.fs.walk.walk_files`, the walk
Discovery uses, so ``winnow stats`` counts the files Discovery collects
(#366). ``detect`` decides from the content and, only when no content rule
recognises a file, from its extension; results say which. An extension-only
mode (``fast=True``) classifies every file by name without reading content;
results say when it was used.

Sizes and times come from the file that was actually opened (the detection
result's snapshot, or an ``fstat`` of the opened file in extension-only
mode), so one result never mixes two files when a path is replaced during a
scan.

Directory inventories aggregate counts per media type, a total size, the span
of file modification times, how often a file's extension disagrees with its
content, and the entries that were skipped or could not be read. Single-file
inspection adds best-effort technical metadata for the detected media type.
"""

from __future__ import annotations

import os
import stat
from bisect import insort
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Final

from winnow.exceptions import MediaError
from winnow.fs.walk import (
    SKIP_KINDS,
    SYMLINK_LOOP,
    WalkEvent,
    WalkEventKind,
    WalkPolicy,
    walk_files,
)
from winnow.media._detect import detect
from winnow.media._detect_base import DECISION_PREFIX
from winnow.media.registry import (
    DEFAULT_FORMAT_REGISTRY,
    FormatRegistry,
    normalize_extension,
)
from winnow.media.service import DefaultMetadataService
from winnow.models.enums import SymlinkPolicy
from winnow.models.evidence import FileSnapshot
from winnow.models.media import MediaMetadata, MediaType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from winnow.media._detect_base import DetectionResult

__all__ = [
    "LINK_CHANGED",
    "MAX_UNREADABLE_EXAMPLES",
    "Classification",
    "DirectoryStats",
    "ExtensionCheck",
    "FileInfo",
    "InventoryEntry",
    "collect_directory_stats",
    "decided_by_name",
    "extension_check",
    "inspect_file",
    "iter_inventory",
    "iter_regular_files",
]

MAX_UNREADABLE_EXAMPLES: Final[int] = 10
"""Most example paths kept for each kind of read error."""

_NAME_SOURCES: Final[frozenset[str]] = frozenset({"extension", "override"})
"""Signal sources whose decision comes from the file name, not the content."""

_INSPECT_ATTEMPTS: Final[int] = 2
"""How often :func:`inspect_file` reads a file that changes while it is read."""

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

LINK_CHANGED: Final[str] = "link changed while it was read"
"""Event message for a name that no longer resolves to the file it opened."""


class Classification(StrEnum):
    """How an inventory decided each file's media type."""

    CONTENT = auto()
    """By :func:`winnow.media.detect`, the rule Discovery uses."""

    EXTENSION = auto()
    """By the file name's extension only (``fast`` mode)."""


class ExtensionCheck(StrEnum):
    """How a file's extension compares with its detected content."""

    MATCH = auto()
    """Content and extension give the same media type."""

    MISMATCH = auto()
    """Content and extension both give a media type, and they differ."""

    EXTENSION_UNTYPED = auto()
    """The content gives a media type; the extension gives none."""

    CONTENT_UNKNOWN = auto()
    """The content gives no media type, whatever the extension says."""

    NAME_ONLY = auto()
    """No content rule recognised the file; its type comes from its name.

    ``detect`` answered from the extension (its fallback rule) or from a
    user-registered extension override, so nothing confirms the type.
    """


def decided_by_name(detection: DetectionResult) -> bool:
    """Report whether a detection's type was decided by the file name.

    Args:
        detection: Result of :func:`winnow.media.detect`.

    Returns:
        ``True`` when the deciding signal is the extension fallback or a
        registered extension override.
    """
    for signal in detection.signals:
        if signal.detail.startswith(DECISION_PREFIX):
            return signal.source in _NAME_SOURCES
    return False


def extension_check(detection: DetectionResult) -> ExtensionCheck:
    """Compare a detection's content answer with its extension answer.

    ``DetectionResult.agrees_with_extension`` is ``None`` both when the
    content is unknown and when only the extension is, so the two are told
    apart here from the underlying fields. A type decided by the name is
    never reported as a match.

    Args:
        detection: Result of :func:`winnow.media.detect`.

    Returns:
        The comparison.
    """
    content = detection.media_type
    named = detection.extension_media_type
    if content is None:
        return ExtensionCheck.CONTENT_UNKNOWN
    if decided_by_name(detection):
        return ExtensionCheck.NAME_ONLY
    if named is None:
        return ExtensionCheck.EXTENSION_UNTYPED
    return ExtensionCheck.MATCH if content == named else ExtensionCheck.MISMATCH


@dataclass(frozen=True, slots=True)
class DirectoryStats:
    """Aggregate statistics for a directory of media files.

    Totals, type counts, bytes and the modification span cover only files
    that were read. Skipped entries and read errors are counted separately;
    the contents of a directory that could not be listed are not counted or
    estimated.

    The fields after the first six have defaults that describe an
    extension-only result with no skips or read errors, so the original
    six-argument constructor keeps working.

    Args:
        total_files: Files read and classified.
        total_bytes: Their combined size.
        counts_by_type: Files per media type. With content classification,
            only types a content rule decided; types decided by the file
            name are in ``typed_by_name``.
        unknown_count: Files with no media type.
        earliest_modified: Earliest modification time (UTC), if any file.
        latest_modified: Latest modification time (UTC), if any file.
        classified_by: Whether types come from content or the extension.
        extension_mismatch_count: Files whose content and extension give
            different media types; ``None`` when classified by extension.
        extension_untyped_count: Files typed by content whose extension gives
            no type; ``None`` when classified by extension.
        typed_by_name: Files no content rule recognised, per the type their
            name gave; ``None`` when classified by extension.
        unreadable_count: Entries that could not be inspected or read,
            including files that vanished during the walk.
        unreadable_examples: Up to :data:`MAX_UNREADABLE_EXAMPLES` of those
            paths, the first in sorted order.
        unreadable_directory_count: Directories that could not be listed.
        unreadable_directory_examples: Up to
            :data:`MAX_UNREADABLE_EXAMPLES` of those paths, the first in
            sorted order.
        skipped_by_reason: Entries the walk policy skipped, per reason.
    """

    total_files: int
    total_bytes: int
    counts_by_type: Mapping[MediaType, int]
    unknown_count: int
    earliest_modified: datetime | None
    latest_modified: datetime | None
    classified_by: Classification = Classification.EXTENSION
    extension_mismatch_count: int | None = None
    extension_untyped_count: int | None = None
    typed_by_name: Mapping[MediaType, int] | None = None
    unreadable_count: int = 0
    unreadable_examples: tuple[Path, ...] = ()
    unreadable_directory_count: int = 0
    unreadable_directory_examples: tuple[Path, ...] = ()
    skipped_by_reason: Mapping[WalkEventKind, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Whether every admitted entry was read.

        Returns:
            ``False`` when any file or directory could not be read.
        """
        return self.unreadable_count == 0 and self.unreadable_directory_count == 0


@dataclass(frozen=True, slots=True)
class FileInfo:
    """Structured metadata describing a single media file.

    Args:
        path: The file.
        media_type: Detected media type, or ``None`` when unknown.
        extension: Normalized extension of the file name.
        size_bytes: Size in bytes.
        modified: Modification time (UTC).
        accessed: Access time (UTC).
        changed: Inode change time (UTC).
        metadata: Technical metadata for the media type, when readable.
        detection: The content detection result; ``None`` when the file was
            classified by extension only.
    """

    path: Path
    media_type: MediaType | None
    extension: str
    size_bytes: int
    modified: datetime
    accessed: datetime
    changed: datetime
    metadata: MediaMetadata | None
    detection: DetectionResult | None = None


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """One file an inventory read and classified.

    Args:
        path: The file, as reached by the walk.
        size_bytes: Size in bytes of the file that was opened.
        modified: Its modification time (UTC).
        media_type: Media type, or ``None`` when unknown.
        detection: The content detection result; ``None`` when classified by
            extension only.
    """

    path: Path
    size_bytes: int
    modified: datetime
    media_type: MediaType | None
    detection: DetectionResult | None


def _utc_from_ns(nanoseconds: int) -> datetime:
    """Convert nanoseconds since the epoch to a UTC datetime, exactly.

    Args:
        nanoseconds: Time in nanoseconds since the epoch.

    Returns:
        The time, truncated to microseconds.
    """
    return _EPOCH + timedelta(microseconds=nanoseconds // 1000)


def _ignore(event: WalkEvent) -> None:
    """Discard a walk event.

    Args:
        event: The event.
    """
    del event


def _effective_policy(policy: WalkPolicy | None, *, recursive: bool) -> WalkPolicy:
    """Apply ``recursive`` to a walk policy.

    Args:
        policy: Policy given by the caller; defaults to :class:`WalkPolicy`.
        recursive: When ``False``, list the top level only.

    Returns:
        The policy to walk with.
    """
    active = policy if policy is not None else WalkPolicy()
    return active if recursive else replace(active, max_depth=0)


def _event(kind: WalkEventKind, path: Path, message: str) -> WalkEvent:
    """Build an inventory event; read errors are issues, skips are not.

    Args:
        kind: The reason.
        path: The entry.
        message: Why.

    Returns:
        The event.
    """
    return WalkEvent(
        kind=kind,
        path=path,
        message=message,
        is_issue=kind not in SKIP_KINDS,
    )


def _error_text(exc: OSError) -> str:
    """Describe an error for an event message.

    Args:
        exc: The error.

    Returns:
        The message.
    """
    return exc.strerror or str(exc)


def _open_stat(path: Path) -> os.stat_result:
    """Open a file without reading it and return the opened file's status.

    The open proves the file is readable; nothing is read. ``O_NONBLOCK``
    keeps a FIFO swapped in for the file from blocking the open.

    Args:
        path: The file.

    Returns:
        ``fstat`` of the opened file.

    Raises:
        OSError: When the file cannot be opened.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def iter_regular_files(
    directory: Path,
    *,
    recursive: bool = True,
    policy: WalkPolicy | None = None,
    on_event: Callable[[WalkEvent], None] | None = None,
) -> Iterator[Path]:
    """Yield the regular files the walk policy admits under a directory.

    The walk is :func:`winnow.fs.walk.walk_files`, Discovery's walk: hidden
    entries and winnow's working folders are skipped, exclude patterns and
    the symlink policy apply, and entries come sorted by name. Under
    ``FOLLOW`` one file may be yielded under several names (a symlink and
    its target); :func:`iter_inventory` decides which name counts.

    Args:
        directory: Directory to walk.
        recursive: When ``True``, descend into subdirectories (down to the
            policy's ``max_depth``).
        policy: Inclusion rules; defaults to :class:`WalkPolicy` (no
            excludes, symlinks skipped).
        on_event: Receives every entry not yielded, with its reason.

    Yields:
        Paths to regular files.
    """
    active = _effective_policy(policy, recursive=recursive)
    report = on_event if on_event is not None else _ignore
    for entry in walk_files(directory, policy=active, on_event=report):
        path = Path(entry.path)
        try:
            st = entry.stat()
        except OSError as exc:
            report(_event(WalkEventKind.UNREADABLE_ENTRY, path, _error_text(exc)))
            continue
        if not stat.S_ISREG(st.st_mode):
            report(_event(WalkEventKind.NOT_REGULAR, path, "not a regular file"))
            continue
        yield path


_Identity = tuple[int, int]
"""``(st_dev, st_ino)`` of a file that was opened."""


@dataclass(frozen=True, slots=True)
class _Classified:
    """A classified file and the identity of the file actually opened.

    Args:
        entry: The public entry.
        identity: Device and inode of the opened file.
    """

    entry: InventoryEntry
    identity: _Identity


def _classify(
    path: Path,
    *,
    registry: FormatRegistry,
    fast: bool,
    report: Callable[[WalkEvent], None],
) -> _Classified | None:
    """Classify one file, taking its size, time and identity from the opened file.

    Args:
        path: The file.
        registry: Registry for detection or the name lookup.
        fast: When ``True``, classify by name; the file is opened, not read.
        report: Receives the event when the file cannot be read.

    Returns:
        The classified file, or ``None`` when it could not be read.
    """
    if fast:
        try:
            opened = _open_stat(path)
        except OSError as exc:
            report(_event(WalkEventKind.UNREADABLE_ENTRY, path, _error_text(exc)))
            return None
        if not stat.S_ISREG(opened.st_mode):
            report(_event(WalkEventKind.NOT_REGULAR, path, "not a regular file"))
            return None
        entry = InventoryEntry(
            path=path,
            size_bytes=opened.st_size,
            modified=_utc_from_ns(opened.st_mtime_ns),
            media_type=registry.lookup(path.name),
            detection=None,
        )
        return _Classified(entry=entry, identity=(opened.st_dev, opened.st_ino))
    detection = detect(path, registry=registry)
    snapshot = detection.snapshot
    if snapshot is None:
        report(
            _event(WalkEventKind.UNREADABLE_ENTRY, path, "content could not be read"),
        )
        return None
    entry = InventoryEntry(
        path=path,
        size_bytes=snapshot.size,
        modified=_utc_from_ns(snapshot.mtime_ns),
        media_type=detection.media_type,
        detection=detection,
    )
    return _Classified(entry=entry, identity=(snapshot.device, snapshot.inode))


@dataclass(slots=True)
class _Pending:
    """An untyped file held until the walk ends.

    Args:
        first: The first name's entry, the one counted if no name types it.
        others: Later untyped names for the same file.
    """

    first: InventoryEntry
    others: list[Path] = field(default_factory=list)


class _FollowClaims:
    """Pick one name per file under ``FOLLOW``, by Discovery's rule.

    A file is keyed on its resolved path, as in Discovery, so two hard links
    are two files. Each name's resolved path is verified against the device
    and inode of the file that was actually opened: a name whose link
    changed (or stopped resolving) while it was read is reported unreadable
    and counts nothing, and the files it pointed to stay eligible under
    their own names. A name whose type is known claims its file; every later
    name for the file is a duplicate. A name with no type never claims: the
    first untyped name of a file is held until the walk ends and counted
    only when no name for the file gave a type. The other names are
    duplicates.

    Args:
        report: Receives the duplicate and read-error events.
    """

    def __init__(self, report: Callable[[WalkEvent], None]) -> None:
        self._report = report
        self._claimed: set[Path] = set()
        self._pending: dict[Path, _Pending] = {}

    def already_claimed(self, path: Path) -> bool:
        """Report, and skip, a name whose target is already counted.

        Resolves the name before any content is read, so a name for a counted
        file is not detected again. The file opened later has the final say
        for every other name.

        Args:
            path: The name.

        Returns:
            ``True`` when the name was reported as a duplicate.
        """
        try:
            target = path.resolve()
        except (OSError, RuntimeError):
            return False
        if target not in self._claimed:
            return False
        self._duplicate(path)
        return True

    def offer(self, classified: _Classified) -> Iterator[InventoryEntry]:
        """Count a file now, hold it, or report it as a duplicate or unreadable.

        Args:
            classified: A classified file and its opened identity.

        Yields:
            The entry when it claims its file.
        """
        entry = classified.entry
        target = self._verified_target(entry.path, classified.identity)
        if target is None:
            return
        if target in self._claimed:
            self._duplicate(entry.path)
            return
        if entry.media_type is None:
            pending = self._pending.get(target)
            if pending is None:
                self._pending[target] = _Pending(first=entry)
            else:
                pending.others.append(entry.path)
            return
        self._claimed.add(target)
        held = self._pending.pop(target, None)
        if held is not None:
            for loser in (held.first.path, *held.others):
                self._duplicate(loser)
        yield entry

    def unclaimed(self) -> Iterator[InventoryEntry]:
        """Yield one untyped name per file no typed name claimed.

        Yields:
            The first untyped name of each such file, in walk order.
        """
        for held in self._pending.values():
            for loser in held.others:
                self._duplicate(loser)
            yield held.first
        self._pending.clear()

    def _verified_target(self, path: Path, identity: _Identity) -> Path | None:
        """Resolve a name and check it still names the file that was opened.

        Args:
            path: The name.
            identity: Device and inode of the file opened for it.

        Returns:
            The resolved path, or ``None`` after reporting the name unreadable
            when it no longer resolves to the opened file.
        """
        try:
            target = path.resolve()
            status = os.stat(target)
        except OSError as exc:
            self._unreadable(path, _error_text(exc))
            return None
        except RuntimeError:
            self._unreadable(path, SYMLINK_LOOP)
            return None
        if (status.st_dev, status.st_ino) != identity:
            self._unreadable(path, LINK_CHANGED)
            return None
        return target

    def _unreadable(self, path: Path, message: str) -> None:
        """Report a name that could not be paired with the file it read.

        Args:
            path: The name.
            message: Why.
        """
        self._report(_event(WalkEventKind.UNREADABLE_ENTRY, path, message))

    def _duplicate(self, path: Path) -> None:
        """Report a name that does not count.

        Args:
            path: The losing name.
        """
        self._report(
            _event(WalkEventKind.DUPLICATE, path, "symlink target already counted"),
        )


def iter_inventory(
    directory: Path,
    *,
    recursive: bool = True,
    registry: FormatRegistry | None = None,
    policy: WalkPolicy | None = None,
    fast: bool = False,
    on_event: Callable[[WalkEvent], None] | None = None,
) -> Iterator[InventoryEntry]:
    """Walk a directory and classify each regular file once.

    A file that cannot be ``stat``-ed or opened, or whose content detection
    could not read it (it vanished, or permission was denied), is reported
    to ``on_event`` as unreadable and not yielded: it is never typed by name.

    Under ``FOLLOW``, a file (one resolved path) reached under several names
    counts once, under the first name that gives it a media type (Discovery's
    rule); a name whose target is already counted is not read again. A name
    whose link changed while it was read is reported unreadable. Files with
    no type under any name are yielded after the walk, in walk order.

    Args:
        directory: Directory to walk.
        recursive: When ``True``, include files in subdirectories.
        registry: Format registry used for classification. Defaults to the
            shared process-wide registry.
        policy: Inclusion rules; defaults to :class:`WalkPolicy`.
        fast: When ``True``, classify by extension only and read no content.
        on_event: Receives every entry not yielded, with its reason.

    Yields:
        One entry per file read.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    report = on_event if on_event is not None else _ignore
    active_policy = _effective_policy(policy, recursive=recursive)
    claims = (
        _FollowClaims(report)
        if active_policy.symlink_policy is SymlinkPolicy.FOLLOW
        else None
    )
    for path in iter_regular_files(
        directory,
        recursive=recursive,
        policy=policy,
        on_event=report,
    ):
        if claims is not None and claims.already_claimed(path):
            continue
        classified = _classify(
            path,
            registry=active_registry,
            fast=fast,
            report=report,
        )
        if classified is None:
            continue
        if claims is None:
            yield classified.entry
        else:
            yield from claims.offer(classified)
    if claims is not None:
        yield from claims.unclaimed()


class _Examples:
    """An exact count and the first few paths in sorted order."""

    def __init__(self) -> None:
        self.count = 0
        self.paths: list[Path] = []

    def add(self, path: Path) -> None:
        """Count a path, keeping it only if it sorts among the first few.

        Args:
            path: The path.
        """
        self.count += 1
        if len(self.paths) < MAX_UNREADABLE_EXAMPLES or path < self.paths[-1]:
            insort(self.paths, path)
            del self.paths[MAX_UNREADABLE_EXAMPLES:]


class _Tally:
    """Running totals for :func:`collect_directory_stats`."""

    def __init__(self) -> None:
        self.counts: dict[MediaType, int] = defaultdict(int)
        self.by_name: dict[MediaType, int] = defaultdict(int)
        self.total_files = 0
        self.total_bytes = 0
        self.unknown = 0
        self.mismatched = 0
        self.untyped = 0
        self.earliest: datetime | None = None
        self.latest: datetime | None = None
        self.skipped: dict[WalkEventKind, int] = defaultdict(int)
        self.unreadable_files = _Examples()
        self.unreadable_directories = _Examples()

    def record_event(self, event: WalkEvent) -> None:
        """Count an entry the inventory did not read.

        Args:
            event: The walk event.
        """
        if event.kind in SKIP_KINDS:
            self.skipped[event.kind] += 1
        elif event.kind is WalkEventKind.UNREADABLE_DIRECTORY:
            self.unreadable_directories.add(event.path)
        else:
            self.unreadable_files.add(event.path)

    def add(self, entry: InventoryEntry) -> None:
        """Count a file that was read.

        Args:
            entry: The classified file.
        """
        self.total_files += 1
        self.total_bytes += entry.size_bytes
        check = (
            extension_check(entry.detection) if entry.detection is not None else None
        )
        if entry.media_type is None:
            self.unknown += 1
        elif check is ExtensionCheck.NAME_ONLY:
            self.by_name[entry.media_type] += 1
        else:
            self.counts[entry.media_type] += 1
        if check is ExtensionCheck.MISMATCH:
            self.mismatched += 1
        elif check is ExtensionCheck.EXTENSION_UNTYPED:
            self.untyped += 1
        if self.earliest is None or entry.modified < self.earliest:
            self.earliest = entry.modified
        if self.latest is None or entry.modified > self.latest:
            self.latest = entry.modified

    def result(self, *, classified_by: Classification) -> DirectoryStats:
        """Freeze the totals.

        Args:
            classified_by: How the files were classified.

        Returns:
            The statistics.
        """
        by_content = classified_by is Classification.CONTENT
        return DirectoryStats(
            total_files=self.total_files,
            total_bytes=self.total_bytes,
            counts_by_type=dict(self.counts),
            unknown_count=self.unknown,
            earliest_modified=self.earliest,
            latest_modified=self.latest,
            classified_by=classified_by,
            extension_mismatch_count=self.mismatched if by_content else None,
            extension_untyped_count=self.untyped if by_content else None,
            typed_by_name=dict(self.by_name) if by_content else None,
            unreadable_count=self.unreadable_files.count,
            unreadable_examples=tuple(self.unreadable_files.paths),
            unreadable_directory_count=self.unreadable_directories.count,
            unreadable_directory_examples=tuple(self.unreadable_directories.paths),
            skipped_by_reason=dict(self.skipped),
        )


def collect_directory_stats(
    directory: Path,
    *,
    recursive: bool = True,
    registry: FormatRegistry | None = None,
    policy: WalkPolicy | None = None,
    fast: bool = False,
) -> DirectoryStats:
    """Aggregate media statistics for a directory.

    Args:
        directory: Directory to summarize.
        recursive: When ``True``, include files in subdirectories.
        registry: Format registry used to classify files. Defaults to the
            shared process-wide registry.
        policy: Inclusion rules, normally
            :meth:`WalkPolicy.from_config <winnow.fs.walk.WalkPolicy.from_config>`
            of the loaded configuration; defaults to :class:`WalkPolicy`.
        fast: When ``True``, classify by extension only and read no content.

    Returns:
        Aggregate counts, total size, modification-time span, extension
        checks, skips and read errors. Timestamps are expressed in UTC.
    """
    tally = _Tally()
    for entry in iter_inventory(
        directory,
        recursive=recursive,
        registry=registry,
        policy=policy,
        fast=fast,
        on_event=tally.record_event,
    ):
        tally.add(entry)
    classified_by = Classification.EXTENSION if fast else Classification.CONTENT
    return tally.result(classified_by=classified_by)


def _metadata_or_none(
    service: DefaultMetadataService,
    path: Path,
    media_type: MediaType,
) -> MediaMetadata | None:
    """Extract metadata for a known type, degrading to ``None`` on failure.

    Args:
        service: Metadata service dispatching to the type's processor.
        path: File to inspect.
        media_type: The file's media type.

    Returns:
        The extracted metadata, or ``None`` when it cannot be read.
    """
    try:
        return service.extract_for(path, media_type)
    except MediaError:
        return None


def _cannot_read(path: Path, exc: OSError) -> MediaError:
    """Build the error for a file that cannot be read.

    Args:
        path: The file.
        exc: The underlying error.

    Returns:
        The error.
    """
    return MediaError(
        "cannot read file",
        operation="inspect_file",
        file_path=path,
        details={"reason": exc.strerror or str(exc)},
    )


def _not_regular(path: Path) -> MediaError:
    """Build the error for a path that is not a regular file.

    Args:
        path: The path.

    Returns:
        The error.
    """
    return MediaError("not a regular file", operation="inspect_file", file_path=path)


def _regular_stat(path: Path) -> os.stat_result:
    """``stat`` a path and require a regular file, without opening it.

    Args:
        path: The file.

    Returns:
        Its status.

    Raises:
        MediaError: When it cannot be ``stat``-ed or is not a regular file.
    """
    try:
        status = path.stat()
    except OSError as exc:
        raise _cannot_read(path, exc) from exc
    if not stat.S_ISREG(status.st_mode):
        raise _not_regular(path)
    return status


def _same_file(path: Path, status: os.stat_result, snapshot: FileSnapshot) -> bool:
    """Report whether a ``stat`` and a snapshot describe the same file state.

    Args:
        path: The path both describe.
        status: A pathname ``stat``.
        snapshot: The opened file's snapshot.

    Returns:
        ``True`` when no field of
        :data:`~winnow.models.evidence.FRESHNESS_FIELDS` differs: path, size,
        modification time, inode change time, device and inode.
    """
    return not FileSnapshot.from_stat(path, status).differences(snapshot)


def _inspect_by_name(path: Path, registry: FormatRegistry) -> FileInfo:
    """Inspect a file by its name only: opened, never read.

    Args:
        path: The file.
        registry: Registry for the name lookup.

    Returns:
        The file's information, with no metadata and no detection.

    Raises:
        MediaError: When the file cannot be opened or is not regular.
    """
    _regular_stat(path)
    try:
        opened = _open_stat(path)
    except OSError as exc:
        raise _cannot_read(path, exc) from exc
    if not stat.S_ISREG(opened.st_mode):
        raise _not_regular(path)
    return FileInfo(
        path=path,
        media_type=registry.lookup(path.name),
        extension=normalize_extension(path.name),
        size_bytes=opened.st_size,
        modified=_utc_from_ns(opened.st_mtime_ns),
        accessed=_utc_from_ns(opened.st_atime_ns),
        changed=_utc_from_ns(opened.st_ctime_ns),
        metadata=None,
    )


def _inspect_once(
    path: Path,
    *,
    registry: FormatRegistry,
    service: DefaultMetadataService,
) -> FileInfo | None:
    """Detect and describe a file once, checking it did not change meanwhile.

    Args:
        path: The file.
        registry: Registry for detection.
        service: Metadata service for the detected type.

    Returns:
        The file's information, or ``None`` when the file at ``path``
        changed between the ``stat``, the detection and the metadata read.

    Raises:
        MediaError: When the file cannot be read or is not regular.
    """
    before = _regular_stat(path)
    detection = detect(path, registry=registry)
    snapshot = detection.snapshot
    if snapshot is None:
        raise MediaError(
            "cannot read file content",
            operation="inspect_file",
            file_path=path,
        )
    if not _same_file(path, before, snapshot):
        return None
    media_type = detection.media_type
    metadata = (
        None if media_type is None else _metadata_or_none(service, path, media_type)
    )
    try:
        after = path.stat()
    except OSError:
        return None
    if not _same_file(path, after, snapshot):
        return None
    return FileInfo(
        path=path,
        media_type=media_type,
        extension=normalize_extension(path.name),
        size_bytes=snapshot.size,
        modified=_utc_from_ns(snapshot.mtime_ns),
        accessed=_utc_from_ns(before.st_atime_ns),
        changed=_utc_from_ns(snapshot.ctime_ns),
        metadata=metadata,
        detection=detection,
    )


def inspect_file(
    path: Path,
    *,
    registry: FormatRegistry | None = None,
    fast: bool = False,
    metadata_service: DefaultMetadataService | None = None,
) -> FileInfo:
    """Gather metadata describing a single file.

    The media type comes from :func:`winnow.media.detect`, run once per
    attempt; the metadata for that type (image, video or audio) is then
    extracted without detecting again, and degrades to ``None`` when it
    cannot be read. Size and times come from the file detection opened. If
    the file at ``path`` is replaced while it is read, the inspection is
    repeated once and then fails, so a result never mixes two files.

    With ``fast=True`` the file is opened to prove it is readable but not
    read: the type comes from the name and no metadata is extracted.

    Args:
        path: File to inspect.
        registry: Format registry used to classify the file. Defaults to the
            shared process-wide registry.
        fast: When ``True``, classify by extension only.
        metadata_service: Service extracting metadata for the detected type.
            Defaults to a :class:`DefaultMetadataService` on ``registry``.

    Returns:
        Structured metadata for the file. Timestamps are expressed in UTC.

    Raises:
        MediaError: When the file cannot be ``stat``-ed or opened, is not a
            regular file, its content cannot be read, or it keeps changing
            while it is read.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    if fast:
        return _inspect_by_name(path, active_registry)
    service = (
        metadata_service
        if metadata_service is not None
        else DefaultMetadataService(registry=active_registry)
    )
    for _ in range(_INSPECT_ATTEMPTS):
        file_info = _inspect_once(path, registry=active_registry, service=service)
        if file_info is not None:
            return file_info
    raise MediaError(
        "file changed while it was read",
        operation="inspect_file",
        file_path=path,
    )
