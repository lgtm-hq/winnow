"""Discovery step: walk the source root and collect media files.

:class:`DiscoveryStep` performs a deterministic, iterative walk of
``state.source`` through :func:`winnow.fs.walk.walk_files` (the walk policy
``winnow stats`` shares), selects media through
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
from datetime import datetime
from pathlib import Path

from winnow.fs.walk import WalkEvent, WalkPolicy, walk_files
from winnow.media.registry import detect_media_type
from winnow.models.config import WinnowConfig
from winnow.models.enums import SymlinkPolicy
from winnow.models.media import MediaFile
from winnow.models.pipeline import PipelineStep
from winnow.pipeline.context import PipelineContext
from winnow.pipeline.events import StepCompleted, StepProgress, StepStarted
from winnow.pipeline.state import RunState

_PROGRESS_INTERVAL = 100


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
    """State for one Discovery run over the shared walk.

    Args:
        state: Run state receiving files and issues.
        config: Configuration supplying depth, excludes and symlink policy.
    """

    def __init__(self, *, state: RunState, config: WinnowConfig) -> None:
        self._state = state
        self._root = state.source
        self._policy = WalkPolicy.from_config(config)
        # Resolved path of every inventoried file mapped to the entry that
        # inventoried it, used under FOLLOW so that a symlink resolving to an
        # already-collected file is reported instead of duplicated. Keyed on
        # the resolved path rather than (st_dev, st_ino): DirEntry.stat()
        # zeroes those on Windows, and hard links are distinct paths.
        self._seen: dict[Path, Path] = {}
        self._accepted = 0

    def run(self) -> None:
        """Walk the root, files before subdirectories, entries sorted by name."""
        for entry in walk_files(
            self._root,
            policy=self._policy,
            on_event=self._on_walk_event,
        ):
            self._collect(entry)

    def _on_walk_event(self, event: WalkEvent) -> None:
        """Record a walk problem as an issue; silent skips are dropped.

        Args:
            event: An entry the walk did not pass on.
        """
        if event.is_issue:
            self._issue(message=event.message, path=event.path)

    def _collect(self, entry: os.DirEntry[str]) -> None:
        """Stat a file entry and record it when it is regular media.

        Non-regular files (FIFOs, sockets, device nodes) are ignored without an
        issue, as :func:`winnow.media.iter_regular_files` skips them. Under
        ``FOLLOW`` a file whose resolved path was already inventoried is
        skipped and recorded as an issue against the symlink that led to it.

        Args:
            entry: Non-directory entry that passed the filters.
        """
        path = Path(entry.path)
        try:
            st = entry.stat()
            if not stat.S_ISREG(st.st_mode):
                return
            media = _build_media_file(path, st=st)
            if media is None or self._already_seen(entry=entry, resolved=media.path):
                return
        except OSError as exc:
            self._issue(message=exc.strerror or str(exc), path=path)
            return
        if self._policy.symlink_policy is SymlinkPolicy.FOLLOW:
            self._seen[media.path] = path
        self._state.files.append(media)
        self._accepted += 1
        if self._accepted % _PROGRESS_INTERVAL == 0:
            self._progress()

    def _already_seen(self, *, entry: os.DirEntry[str], resolved: Path) -> bool:
        """Report whether ``entry`` resolves to a file inventoried earlier.

        Only meaningful under ``FOLLOW``; other policies never pass a symlink
        to :meth:`_collect`, so resolved paths are not tracked for them. Two
        entries can only share a resolved path through a symlink, so the issue
        is recorded against whichever of the two entries is the link: the walk
        visits files in name order and a link may sort before its target.

        Args:
            entry: Directory entry under consideration.
            resolved: Resolved path of ``entry``.

        Returns:
            ``True`` when the file was already inventoried and an issue has
            been recorded.
        """
        if self._policy.symlink_policy is not SymlinkPolicy.FOLLOW:
            return False
        existing = self._seen.get(resolved)
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
