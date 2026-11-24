"""Directory inventory and per-file inspection.

Files are identified by their content with :func:`winnow.media.detect`, the
rule Discovery uses, and enumerated with :func:`winnow.fs.walk.walk_files`,
the walk Discovery uses, so ``winnow stats`` counts the files Discovery
collects (#366). An extension-only mode (``fast=True``) classifies by name
without reading content; results say when it was used.

Directory inventories aggregate counts per media type, a total size, the span
of file modification times, how often a file's extension disagrees with its
content, and the entries that were skipped or could not be read. Single-file
inspection adds best-effort technical metadata for the detected media type.
"""

from __future__ import annotations

import stat
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Final

from winnow.exceptions import MediaError
from winnow.fs.walk import SKIP_KINDS, WalkEvent, WalkEventKind, WalkPolicy, walk_files
from winnow.media._detect import detect
from winnow.media.registry import (
    DEFAULT_FORMAT_REGISTRY,
    FormatRegistry,
    normalize_extension,
)
from winnow.media.service import DefaultMetadataService
from winnow.models.enums import SymlinkPolicy
from winnow.models.media import MediaMetadata, MediaType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from winnow.media._detect_base import DetectionResult

__all__ = [
    "MAX_UNREADABLE_EXAMPLES",
    "Classification",
    "DirectoryStats",
    "ExtensionCheck",
    "FileInfo",
    "InventoryEntry",
    "collect_directory_stats",
    "extension_check",
    "inspect_file",
    "iter_inventory",
    "iter_regular_files",
]

MAX_UNREADABLE_EXAMPLES: Final[int] = 10
"""Most example paths kept for each kind of read error."""


class Classification(StrEnum):
    """How an inventory decided each file's media type."""

    CONTENT = auto()
    """By content detection, the rule Discovery uses."""

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


def extension_check(detection: DetectionResult) -> ExtensionCheck:
    """Compare a detection's content answer with its extension answer.

    ``DetectionResult.agrees_with_extension`` is ``None`` both when the
    content is unknown and when only the extension is, so the two are told
    apart here from the underlying fields.

    Args:
        detection: Result of :func:`winnow.media.detect`.

    Returns:
        The comparison.
    """
    content = detection.media_type
    named = detection.extension_media_type
    if content is None:
        return ExtensionCheck.CONTENT_UNKNOWN
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

    Args:
        total_files: Files read and classified.
        total_bytes: Their combined size.
        counts_by_type: Files per detected media type.
        unknown_count: Files with no media type.
        earliest_modified: Earliest modification time (UTC), if any file.
        latest_modified: Latest modification time (UTC), if any file.
        classified_by: Whether types come from content or the extension.
        extension_mismatch_count: Files whose content and extension give
            different media types; ``None`` when classified by extension.
        extension_untyped_count: Files typed by content whose extension gives
            no type; ``None`` when classified by extension.
        unreadable_count: Entries that could not be inspected or read,
            including files that vanished during the walk.
        unreadable_examples: Up to :data:`MAX_UNREADABLE_EXAMPLES` of those
            paths, sorted.
        unreadable_directory_count: Directories that could not be listed.
        unreadable_directory_examples: Up to
            :data:`MAX_UNREADABLE_EXAMPLES` of those paths, sorted.
        skipped_by_reason: Entries the walk policy skipped, per reason.
    """

    total_files: int
    total_bytes: int
    counts_by_type: Mapping[MediaType, int]
    unknown_count: int
    earliest_modified: datetime | None
    latest_modified: datetime | None
    classified_by: Classification
    extension_mismatch_count: int | None
    extension_untyped_count: int | None
    unreadable_count: int
    unreadable_examples: tuple[Path, ...]
    unreadable_directory_count: int
    unreadable_directory_examples: tuple[Path, ...]
    skipped_by_reason: Mapping[WalkEventKind, int]

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
        size_bytes: Size in bytes.
        modified: Modification time (UTC).
        media_type: Media type, or ``None`` when unknown.
        detection: The content detection result; ``None`` when classified by
            extension only.
    """

    path: Path
    size_bytes: int
    modified: datetime
    media_type: MediaType | None
    detection: DetectionResult | None


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


def _unreadable(path: Path, message: str) -> WalkEvent:
    """Build the event for an entry that could not be read.

    Args:
        path: The entry.
        message: Why.

    Returns:
        The event.
    """
    return WalkEvent(
        kind=WalkEventKind.UNREADABLE_ENTRY,
        path=path,
        message=message,
        is_issue=True,
    )


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
    ``FOLLOW`` a file reached a second time through a symlink is yielded
    once.

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
    follow = active.symlink_policy is SymlinkPolicy.FOLLOW
    seen: set[Path] = set()
    for entry in walk_files(directory, policy=active, on_event=report):
        path = Path(entry.path)
        try:
            st = entry.stat()
            resolved = path.resolve() if follow else path
        except OSError as exc:
            report(_unreadable(path, exc.strerror or str(exc)))
            continue
        if not stat.S_ISREG(st.st_mode):
            report(
                WalkEvent(
                    kind=WalkEventKind.NOT_REGULAR,
                    path=path,
                    message="not a regular file",
                    is_issue=False,
                ),
            )
            continue
        if follow:
            if resolved in seen:
                report(
                    WalkEvent(
                        kind=WalkEventKind.DUPLICATE,
                        path=path,
                        message="symlink target already counted",
                        is_issue=False,
                    ),
                )
                continue
            seen.add(resolved)
        yield path


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

    A file that cannot be ``stat``-ed, or whose content detection could not
    read it (it vanished, or permission was denied), is reported to
    ``on_event`` as unreadable and not yielded: it is never typed by name.

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
    for path in iter_regular_files(
        directory,
        recursive=recursive,
        policy=policy,
        on_event=report,
    ):
        try:
            st = path.stat()
        except OSError as exc:
            report(_unreadable(path, exc.strerror or str(exc)))
            continue
        detection: DetectionResult | None = None
        if fast:
            media_type = active_registry.lookup(path.name)
        else:
            detection = detect(path, registry=active_registry)
            if detection.snapshot is None:
                report(_unreadable(path, "content could not be read"))
                continue
            media_type = detection.media_type
        yield InventoryEntry(
            path=path,
            size_bytes=st.st_size,
            modified=datetime.fromtimestamp(st.st_mtime, tz=UTC),
            media_type=media_type,
            detection=detection,
        )


class _Tally:
    """Running totals for :func:`collect_directory_stats`."""

    def __init__(self) -> None:
        self.counts: dict[MediaType, int] = defaultdict(int)
        self.total_files = 0
        self.total_bytes = 0
        self.unknown = 0
        self.mismatched = 0
        self.untyped = 0
        self.earliest: datetime | None = None
        self.latest: datetime | None = None
        self.skipped: dict[WalkEventKind, int] = defaultdict(int)
        self.unreadable_files: list[Path] = []
        self.unreadable_directories: list[Path] = []

    def record_event(self, event: WalkEvent) -> None:
        """Count an entry the inventory did not read.

        Args:
            event: The walk event.
        """
        if event.kind in SKIP_KINDS:
            self.skipped[event.kind] += 1
        elif event.kind is WalkEventKind.UNREADABLE_DIRECTORY:
            self.unreadable_directories.append(event.path)
        else:
            self.unreadable_files.append(event.path)

    def add(self, entry: InventoryEntry) -> None:
        """Count a file that was read.

        Args:
            entry: The classified file.
        """
        self.total_files += 1
        self.total_bytes += entry.size_bytes
        if entry.media_type is None:
            self.unknown += 1
        else:
            self.counts[entry.media_type] += 1
        if entry.detection is not None:
            check = extension_check(entry.detection)
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
            unreadable_count=len(self.unreadable_files),
            unreadable_examples=_examples(self.unreadable_files),
            unreadable_directory_count=len(self.unreadable_directories),
            unreadable_directory_examples=_examples(self.unreadable_directories),
            skipped_by_reason=dict(self.skipped),
        )


def _examples(paths: list[Path]) -> tuple[Path, ...]:
    """Pick the first example paths in sorted order.

    Args:
        paths: Every path of one kind.

    Returns:
        At most :data:`MAX_UNREADABLE_EXAMPLES` paths, sorted.
    """
    return tuple(sorted(paths)[:MAX_UNREADABLE_EXAMPLES])


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


def inspect_file(
    path: Path,
    *,
    registry: FormatRegistry | None = None,
    fast: bool = False,
    metadata_service: DefaultMetadataService | None = None,
) -> FileInfo:
    """Gather metadata describing a single file.

    The media type comes from content detection, run once; the metadata for
    that type (image, video or audio) is then extracted without detecting
    again, and degrades to ``None`` when it cannot be read.

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
        MediaError: When the file cannot be ``stat``-ed, is not a regular
            file, or its content cannot be read.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise MediaError(
            "cannot read file",
            operation="inspect_file",
            file_path=path,
            details={"reason": exc.strerror or str(exc)},
        ) from exc
    if not stat.S_ISREG(stat_result.st_mode):
        raise MediaError(
            "not a regular file",
            operation="inspect_file",
            file_path=path,
        )
    detection: DetectionResult | None = None
    if fast:
        media_type = active_registry.lookup(path.name)
    else:
        detection = detect(path, registry=active_registry)
        if detection.snapshot is None:
            raise MediaError(
                "cannot read file content",
                operation="inspect_file",
                file_path=path,
            )
        media_type = detection.media_type
    service = (
        metadata_service
        if metadata_service is not None
        else DefaultMetadataService(registry=active_registry)
    )
    metadata = (
        None if media_type is None else _metadata_or_none(service, path, media_type)
    )
    return FileInfo(
        path=path,
        media_type=media_type,
        extension=normalize_extension(path.name),
        size_bytes=stat_result.st_size,
        modified=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
        accessed=datetime.fromtimestamp(stat_result.st_atime, tz=UTC),
        changed=datetime.fromtimestamp(stat_result.st_ctime, tz=UTC),
        metadata=metadata,
        detection=detection,
    )
