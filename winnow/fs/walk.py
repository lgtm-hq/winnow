"""Read-only directory walk shared by Discovery and the inventory commands.

:func:`walk_files` applies one inclusion policy wherever winnow enumerates a
library: dot-prefixed entries and winnow's own working folders are skipped,
configured exclude patterns are applied, symlinks are admitted or refused by
the configured :class:`~winnow.models.enums.SymlinkPolicy`, the depth limit is
honoured, and entries are visited sorted by name with a directory's files
before its subdirectories. Nothing is opened or read; problems are reported to
a callback as :class:`WalkEvent` values and never abort the walk.

Discovery (:mod:`winnow.pipeline.steps.discovery`) and ``winnow stats``
(:func:`winnow.media.inventory.collect_directory_stats`) both walk through
this module, so the two see the same files (#366).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Final

from winnow.exceptions import SecurityError
from winnow.models.enums import SymlinkPolicy
from winnow.security.path_validator import PathValidator

if TYPE_CHECKING:
    from winnow.models.config import WinnowConfig

__all__ = [
    "ALWAYS_SKIPPED",
    "SKIP_KINDS",
    "WalkEvent",
    "WalkEventKind",
    "WalkPolicy",
    "is_always_skipped",
    "is_excluded",
    "walk_files",
]

ALWAYS_SKIPPED: Final[frozenset[str]] = frozenset(
    {".winnow-backups", ".winnow-staging"},
)
"""Winnow's own working folders, never walked into."""


class WalkEventKind(StrEnum):
    """Why an entry was not passed on by a walk."""

    HIDDEN = auto()
    """A dot-prefixed entry or one of winnow's working folders."""

    EXCLUDED = auto()
    """Matched a configured exclude pattern."""

    SYMLINK = auto()
    """A symlink the symlink policy refused."""

    NOT_REGULAR = auto()
    """A FIFO, socket or device node."""

    DUPLICATE = auto()
    """Under ``FOLLOW``, a symlink to a file already passed on."""

    UNREADABLE_DIRECTORY = auto()
    """A directory that could not be listed; its contents are unknown."""

    UNREADABLE_ENTRY = auto()
    """An entry that could not be inspected or read."""


SKIP_KINDS: Final[frozenset[WalkEventKind]] = frozenset(
    {
        WalkEventKind.HIDDEN,
        WalkEventKind.EXCLUDED,
        WalkEventKind.SYMLINK,
        WalkEventKind.NOT_REGULAR,
        WalkEventKind.DUPLICATE,
    },
)
"""Kinds that are a deliberate skip rather than a read error."""


@dataclass(frozen=True, slots=True)
class WalkEvent:
    """One entry a walk did not pass on, and why.

    Args:
        kind: The reason.
        path: The entry, as reached by the walk (not resolved).
        message: Human-readable detail, such as the ``OSError`` text.
        is_issue: Whether Discovery records this as a run issue. Silent
            skips (hidden, excluded, symlinks under ``SKIP``) are not issues.
    """

    kind: WalkEventKind
    path: Path
    message: str
    is_issue: bool


@dataclass(frozen=True, slots=True)
class WalkPolicy:
    """Which entries a walk admits.

    Args:
        exclude_patterns: Glob patterns tested against each entry's name and
            its POSIX path relative to the walk root.
        symlink_policy: How symlinks are treated.
        max_depth: Deepest directory level descended into below the root
            (``0`` lists the root only); ``None`` for no limit.
    """

    exclude_patterns: tuple[str, ...] = ()
    symlink_policy: SymlinkPolicy = SymlinkPolicy.SKIP
    max_depth: int | None = None

    @classmethod
    def from_config(
        cls,
        config: WinnowConfig,
        *,
        recursive: bool = True,
    ) -> WalkPolicy:
        """Build the policy Discovery uses from a configuration.

        Args:
            config: Loaded configuration.
            recursive: When ``False``, list the root only.

        Returns:
            The policy.
        """
        return cls(
            exclude_patterns=tuple(config.paths.exclude_patterns),
            symlink_policy=config.symlink_policy,
            max_depth=config.organize.max_depth if recursive else 0,
        )


def is_excluded(
    *,
    name: str,
    relative: str,
    patterns: Sequence[str],
) -> bool:
    """Report whether an entry matches any exclude pattern.

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


def is_always_skipped(name: str) -> bool:
    """Report whether an entry is skipped regardless of configuration.

    Args:
        name: Bare entry name.

    Returns:
        ``True`` for dot-prefixed entries and winnow's own working folders.
    """
    return name.startswith(".") or name in ALWAYS_SKIPPED


def walk_files(
    root: Path,
    *,
    policy: WalkPolicy,
    on_event: Callable[[WalkEvent], None],
) -> Iterator[os.DirEntry[str]]:
    """Yield every non-directory entry the policy admits under ``root``.

    The walk is iterative, visits entries sorted by name, yields a
    directory's files before descending into its subdirectories, and never
    visits a resolved directory twice. An admitted entry may be a symlink
    (under ``FOLLOW``) or a non-regular file; callers ``stat`` it.

    Args:
        root: Directory to walk.
        policy: Inclusion rules.
        on_event: Receives every entry not yielded, with its reason.

    Yields:
        Admitted non-directory entries.
    """
    yield from _Walker(root=root, policy=policy, on_event=on_event).run()


class _Walker:
    """State for one walk.

    Args:
        root: Directory to walk.
        policy: Inclusion rules.
        on_event: Receives every entry not yielded.
    """

    def __init__(
        self,
        *,
        root: Path,
        policy: WalkPolicy,
        on_event: Callable[[WalkEvent], None],
    ) -> None:
        self._root = root
        self._policy = policy
        self._on_event = on_event
        # SKIP and ERROR both make the validator refuse symlinks; the policy
        # only decides whether the refusal is reported as an issue below.
        self._validator = PathValidator(
            [root],
            symlink_policy=policy.symlink_policy,
        )
        self._visited: set[Path] = set()

    def run(self) -> Iterator[os.DirEntry[str]]:
        """Walk the root, files before subdirectories, entries sorted by name.

        Yields:
            Admitted non-directory entries.
        """
        stack: list[tuple[Path, int]] = [(self._root, 0)]
        while stack:
            directory, depth = stack.pop()
            resolved = directory.resolve()
            if resolved in self._visited:
                continue
            self._visited.add(resolved)
            subdirs: list[Path] = []
            yield from self._visit(directory=directory, depth=depth, subdirs=subdirs)
            stack.extend((subdir, depth + 1) for subdir in reversed(subdirs))

    def _visit(
        self,
        *,
        directory: Path,
        depth: int,
        subdirs: list[Path],
    ) -> Iterator[os.DirEntry[str]]:
        """Yield one directory's admitted files and collect its subdirectories.

        Args:
            directory: Directory to scan.
            depth: Depth of ``directory`` below the root (root is 0).
            subdirs: Receives the subdirectories to descend into, in order.

        Yields:
            Admitted non-directory entries of ``directory``.
        """
        try:
            with os.scandir(directory) as handle:
                entries = sorted(handle, key=lambda entry: entry.name)
        except OSError as exc:
            self._report(WalkEventKind.UNREADABLE_DIRECTORY, directory, exc)
            return
        max_depth = self._policy.max_depth
        descend = max_depth is None or depth < max_depth
        for entry in entries:
            if self._skipped(entry) or not self._admit_symlink(entry):
                continue
            is_dir = self._is_dir(entry)
            if is_dir is None:
                continue
            if is_dir:
                if descend:
                    subdirs.append(Path(entry.path))
            else:
                yield entry

    def _skipped(self, entry: os.DirEntry[str]) -> bool:
        """Report, and skip, an entry excluded by name or pattern.

        Args:
            entry: Directory entry under consideration.

        Returns:
            ``True`` when the entry must not be processed.
        """
        if is_always_skipped(entry.name):
            self._skip(WalkEventKind.HIDDEN, entry, "hidden or winnow folder")
            return True
        patterns = self._policy.exclude_patterns
        if not patterns:
            return False
        relative = Path(entry.path).relative_to(self._root).as_posix()
        if is_excluded(name=entry.name, relative=relative, patterns=patterns):
            self._skip(WalkEventKind.EXCLUDED, entry, "matches an exclude pattern")
            return True
        return False

    def _admit_symlink(self, entry: os.DirEntry[str]) -> bool:
        """Apply the symlink policy to an entry.

        Regular entries are always admitted. Symlinks pass through the
        :class:`PathValidator`; a refusal is a silent skip under ``SKIP`` and
        an issue under ``FOLLOW`` (escaping target) or ``ERROR``.

        Args:
            entry: Directory entry under consideration.

        Returns:
            ``True`` when the walk may treat the entry as its target.
        """
        path = Path(entry.path)
        try:
            if not entry.is_symlink():
                return True
            self._validator.validate_path(entry.path, operation="discovery")
        except SecurityError as exc:
            self._on_event(
                WalkEvent(
                    kind=WalkEventKind.SYMLINK,
                    path=path,
                    message=exc.message,
                    is_issue=self._policy.symlink_policy is not SymlinkPolicy.SKIP,
                ),
            )
            return False
        except OSError as exc:
            self._report(WalkEventKind.UNREADABLE_ENTRY, path, exc)
            return False
        return True

    def _is_dir(self, entry: os.DirEntry[str]) -> bool | None:
        """Report whether an admitted entry is a directory.

        Args:
            entry: Directory entry that passed the symlink policy.

        Returns:
            ``True`` for directories (following admitted symlinks), ``False``
            otherwise, and ``None`` when the entry could not be inspected.
        """
        try:
            return entry.is_dir()
        except OSError as exc:
            self._report(WalkEventKind.UNREADABLE_ENTRY, Path(entry.path), exc)
            return None

    def _skip(
        self,
        kind: WalkEventKind,
        entry: os.DirEntry[str],
        message: str,
    ) -> None:
        """Report a silent skip.

        Args:
            kind: The reason.
            entry: The skipped entry.
            message: Human-readable detail.
        """
        self._on_event(
            WalkEvent(
                kind=kind, path=Path(entry.path), message=message, is_issue=False
            ),
        )

    def _report(self, kind: WalkEventKind, path: Path, exc: OSError) -> None:
        """Report a read error as an issue.

        Args:
            kind: Whether a directory or an entry failed.
            path: The path that failed.
            exc: The error.
        """
        self._on_event(
            WalkEvent(
                kind=kind,
                path=path,
                message=exc.strerror or str(exc),
                is_issue=True,
            ),
        )
