"""Discovery step: walk the source root and collect media files.

:class:`DiscoveryStep` performs a deterministic, iterative walk of
``state.source``, selects media through
:func:`winnow.media.registry.detect_media_type`, and appends a
:class:`~winnow.models.media.MediaFile` for each accepted file to
``state.files``. Per-entry problems (unreadable directories, failed ``stat``
calls, symlinks that violate the configured policy) are recorded through
:meth:`~winnow.pipeline.state.RunState.record_issue` and never abort the walk.
Only regular files are inventoried (FIFOs, sockets and device nodes are ignored
without an issue), and under ``FOLLOW`` a file identity is inventoried at most
once even when several symlinks resolve to it.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Sequence
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

from winnow.exceptions import SecurityError
from winnow.media.registry import detect_media_type
from winnow.models.config import WinnowConfig
from winnow.models.enums import SymlinkPolicy
from winnow.models.media import MediaFile
from winnow.models.pipeline import PipelineStep
from winnow.pipeline.context import PipelineContext
from winnow.pipeline.events import StepCompleted, StepProgress, StepStarted
from winnow.pipeline.state import RunState
from winnow.security.path_validator import PathValidator

_ALWAYS_SKIPPED: frozenset[str] = frozenset({".winnow-backups", ".winnow-staging"})
_PROGRESS_INTERVAL = 100


def _is_excluded(
    *,
    name: str,
    relative: str,
    patterns: Sequence[str],
) -> bool:
    """Report whether an entry matches any configured exclude pattern.

    Mirrors the semantics of ``winnow.cli.clean._is_excluded``: a pattern is
    tested against the entry name and against its POSIX path relative to the
    walk root. Because the walk prunes excluded directories, ancestors do not
    need to be re-checked here.

    Args:
        name: Bare entry name.
        relative: Entry path relative to the walk root, with forward slashes.
        patterns: Glob patterns identifying entries to exclude.

    Returns:
        ``True`` when the entry should be skipped.
    """
    return any(fnmatch(name, p) or fnmatch(relative, p) for p in patterns)


def _is_always_skipped(name: str) -> bool:
    """Report whether an entry is skipped regardless of configuration.

    Args:
        name: Bare entry name.

    Returns:
        ``True`` for dot-prefixed entries and winnow's own working folders.
    """
    return name.startswith(".") or name in _ALWAYS_SKIPPED


def _build_media_file(path: Path, *, st: os.stat_result) -> MediaFile | None:
    """Build a MediaFile for ``path`` when it is a recognised media file.

    Args:
        path: Candidate file path as encountered during the walk.
        st: Result of a following ``stat`` on ``path``.

    Returns:
        The populated model, or ``None`` when the file is not media.
    """
    media_type = detect_media_type(path)
    if media_type is None:
        return None
    return MediaFile(
        path=path.resolve(),
        media_type=media_type,
        extension=path.suffix.lower(),
        size_bytes=st.st_size,
        creation_date=datetime.fromtimestamp(st.st_mtime).astimezone(),
    )


class _Walk:
    """State for one iterative directory walk.

    Args:
        state: Run state receiving files and issues.
        config: Configuration supplying depth, excludes and symlink policy.
    """

    def __init__(self, *, state: RunState, config: WinnowConfig) -> None:
        self._state = state
        self._root = state.source
        self._max_depth = config.organize.max_depth
        self._patterns = tuple(config.paths.exclude_patterns)
        self._policy = config.symlink_policy
        # SKIP and ERROR both make the validator refuse symlinks; the policy
        # only decides whether the refusal is recorded as an issue below.
        self._validator = PathValidator(
            [self._root],
            symlink_policy=config.symlink_policy,
        )
        self._visited: set[Path] = set()
        # Identity of every inventoried file mapped to the entry that
        # inventoried it, used under FOLLOW so that a symlink resolving to an
        # already-collected file is reported instead of duplicated.
        self._seen: dict[tuple[int, int], Path] = {}
        self._accepted = 0

    def run(self) -> None:
        """Walk the root, files before subdirectories, entries sorted by name."""
        stack: list[tuple[Path, int]] = [(self._root, 0)]
        while stack:
            directory, depth = stack.pop()
            resolved = directory.resolve()
            if resolved in self._visited:
                continue
            self._visited.add(resolved)
            subdirs = self._visit(directory=directory, depth=depth)
            stack.extend((subdir, depth + 1) for subdir in reversed(subdirs))

    def _visit(self, *, directory: Path, depth: int) -> list[Path]:
        """Process one directory's files and return its traversable children.

        Args:
            directory: Directory to scan.
            depth: Depth of ``directory`` below the root (root is 0).

        Returns:
            Subdirectories to descend into, in name order.
        """
        try:
            with os.scandir(directory) as handle:
                entries = sorted(handle, key=lambda entry: entry.name)
        except OSError as exc:
            self._issue(message=exc.strerror or str(exc), path=directory)
            return []
        descend = self._max_depth is None or depth < self._max_depth
        subdirs: list[Path] = []
        for entry in entries:
            if self._skip_entry(entry) or not self._admit_symlink(entry):
                continue
            if self._is_dir(entry):
                if descend:
                    subdirs.append(Path(entry.path))
            else:
                self._collect(entry)
        return subdirs

    def _skip_entry(self, entry: os.DirEntry[str]) -> bool:
        """Report whether an entry is excluded by name or pattern.

        Args:
            entry: Directory entry under consideration.

        Returns:
            ``True`` when the entry must not be processed.
        """
        if _is_always_skipped(entry.name):
            return True
        if not self._patterns:
            return False
        relative = Path(entry.path).relative_to(self._root).as_posix()
        return _is_excluded(
            name=entry.name,
            relative=relative,
            patterns=self._patterns,
        )

    def _admit_symlink(self, entry: os.DirEntry[str]) -> bool:
        """Apply the symlink policy to an entry.

        Regular entries are always admitted. Symlinks pass through the
        :class:`PathValidator`; a refusal is silent under ``SKIP`` and recorded
        as an issue under ``FOLLOW`` (escaping target) or ``ERROR``.

        Args:
            entry: Directory entry under consideration.

        Returns:
            ``True`` when the walk may treat the entry as its target.
        """
        try:
            if not entry.is_symlink():
                return True
            self._validator.validate_path(entry.path, operation="discovery")
        except SecurityError as exc:
            if self._policy is not SymlinkPolicy.SKIP:
                self._issue(message=exc.message, path=Path(entry.path))
            return False
        except OSError as exc:
            self._issue(message=exc.strerror or str(exc), path=Path(entry.path))
            return False
        return True

    def _is_dir(self, entry: os.DirEntry[str]) -> bool:
        """Report whether an admitted entry is a directory.

        Args:
            entry: Directory entry that passed the symlink policy.

        Returns:
            ``True`` for directories (following admitted symlinks).
        """
        try:
            return entry.is_dir()
        except OSError as exc:
            self._issue(message=exc.strerror or str(exc), path=Path(entry.path))
            return False

    def _collect(self, entry: os.DirEntry[str]) -> None:
        """Stat a file entry and record it when it is regular media.

        Non-regular files (FIFOs, sockets, device nodes) are ignored without an
        issue, matching :func:`winnow.fs.iter_regular_files`. Under ``FOLLOW``
        a file whose identity was already inventoried is skipped and recorded
        as an issue against the entry that resolved to it.

        Args:
            entry: Non-directory entry that passed the filters.
        """
        path = Path(entry.path)
        try:
            st = entry.stat()
            if not stat.S_ISREG(st.st_mode):
                return
            if self._already_seen(entry=entry, st=st):
                return
            media = _build_media_file(path, st=st)
        except OSError as exc:
            self._issue(message=exc.strerror or str(exc), path=path)
            return
        if media is None:
            return
        if self._policy is SymlinkPolicy.FOLLOW:
            self._seen[(st.st_dev, st.st_ino)] = path
        self._state.files.append(media)
        self._accepted += 1
        if self._accepted % _PROGRESS_INTERVAL == 0:
            self._progress()

    def _already_seen(self, *, entry: os.DirEntry[str], st: os.stat_result) -> bool:
        """Report whether ``entry`` resolves to a file inventoried earlier.

        Only meaningful under ``FOLLOW``; other policies never pass a symlink
        to :meth:`_collect`, so identities are not tracked for them. The issue
        is recorded against the symlink, whichever of the two entries it is,
        because the walk visits files in name order and a link may sort before
        its target.

        Args:
            entry: Directory entry under consideration.
            st: Result of a following ``stat`` on ``entry``.

        Returns:
            ``True`` when the file was already inventoried and an issue has
            been recorded.
        """
        if self._policy is not SymlinkPolicy.FOLLOW:
            return False
        existing = self._seen.get((st.st_dev, st.st_ino))
        if existing is None:
            return False
        path = Path(entry.path)
        link, target = (path, existing) if entry.is_symlink() else (existing, path)
        self._issue(
            message=f"symlink target already inventoried as {target}",
            path=link,
        )
        return True

    def _progress(self) -> None:
        """Emit a StepProgress event with the current accepted count."""
        self._state.events.emit(
            StepProgress(step=PipelineStep.DISCOVERY, current=self._accepted),
        )

    def _issue(self, *, message: str, path: Path) -> None:
        """Record a non-fatal issue against ``path``.

        Args:
            message: Description of the problem.
            path: Path the problem relates to.
        """
        self._state.record_issue(
            step=PipelineStep.DISCOVERY,
            message=message,
            path=path,
        )


class DiscoveryStep:
    """Walk ``state.source`` and fill ``state.files`` with media files.

    The walk is iterative (explicit stack), visits entries sorted by name with
    files before subdirectories, honours ``config.organize.max_depth`` and
    ``config.paths.exclude_patterns``, and applies ``config.symlink_policy``
    through a :class:`~winnow.security.path_validator.PathValidator` built once
    per run. Dot-prefixed entries and winnow's own working folders are always
    skipped.
    """

    @property
    def name(self) -> PipelineStep:
        """Return the step identifier.

        Returns:
            Always :attr:`PipelineStep.DISCOVERY`.
        """
        return PipelineStep.DISCOVERY

    def run(self, state: RunState, *, context: PipelineContext) -> None:
        """Discover media under ``state.source``.

        Args:
            state: Run state; ``files``, ``result`` and ``step_durations`` are
                updated in place.
            context: Service container supplying the run configuration.
        """
        started = time.perf_counter()
        state.events.emit(StepStarted(step=self.name))

        _Walk(state=state, config=context.config).run()

        duration = time.perf_counter() - started
        state.events.emit(StepProgress(step=self.name, current=len(state.files)))
        state.result.total_files_scanned = len(state.files)
        state.result.steps_completed.append(self.name)
        state.step_durations[self.name] = duration
        state.events.emit(StepCompleted(step=self.name, duration_seconds=duration))


__all__ = ["DiscoveryStep"]
