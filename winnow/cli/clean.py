"""``winnow clean`` command for pruning empty directories.

Removal is bottom-up: a directory is removable only when it holds no files
and every subdirectory is itself removable, so clearing nested leaves can
cascade up to their now-empty parents. The target root is always preserved.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

import click

from winnow.cli.console import console_from_context
from winnow.cli.standards import dry_run_option, yes_option

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["clean", "find_empty_directories", "remove_empty_directories"]


def _is_excluded(
    directory: Path,
    *,
    root: Path,
    patterns: Sequence[str],
) -> bool:
    """Report whether a directory matches any exclude pattern.

    Patterns are matched against both the directory name and its path
    relative to ``root`` (using forward slashes), so ``".git"`` and
    ``"cache/*"`` both work as expected. The directory's ancestors below
    ``root`` are checked too, so everything inside an excluded subtree is
    preserved even though ``os.walk(topdown=False)`` visits it first.

    Args:
        directory: Directory being considered for removal.
        root: Root directory the walk started from.
        patterns: Glob patterns identifying directories to preserve.

    Returns:
        ``True`` when the directory should be preserved.
    """
    if not patterns:
        return False
    candidates = [directory, *directory.parents]
    for candidate in candidates:
        if candidate == root:
            break
        relative = candidate.relative_to(root).as_posix()
        name = candidate.name
        if any(
            fnmatch(name, pattern) or fnmatch(relative, pattern) for pattern in patterns
        ):
            return True
    return False


def find_empty_directories(
    root: Path,
    *,
    exclude_patterns: Sequence[str] = (),
) -> list[Path]:
    """Find removable empty directories under ``root``.

    Args:
        root: Directory to search within. It is never itself returned.
        exclude_patterns: Glob patterns for directories to preserve. An
            excluded directory keeps its ancestors from being removed.

    Returns:
        Removable directories ordered so that children precede parents,
        making them safe to delete sequentially.
    """
    removable: set[Path] = set()
    ordered: list[Path] = []
    for current_path, subdir_names, file_names in os.walk(root, topdown=False):
        current = Path(current_path)
        if current == root:
            continue
        if file_names:
            continue
        if _is_excluded(current, root=root, patterns=exclude_patterns):
            continue
        child_dirs = (current / name for name in subdir_names)
        if all(child in removable for child in child_dirs):
            removable.add(current)
            ordered.append(current)
    return ordered


def remove_empty_directories(directories: Sequence[Path]) -> list[Path]:
    """Delete the given directories in order.

    Args:
        directories: Empty directories ordered children-before-parents, as
            produced by :func:`find_empty_directories`.

    Returns:
        The directories that were removed.
    """
    removed: list[Path] = []
    for directory in directories:
        directory.rmdir()
        removed.append(directory)
    return removed


@click.command()
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--exclude",
    "exclude_patterns",
    multiple=True,
    metavar="PATTERN",
    help="Glob pattern for directories to preserve; repeatable.",
)
@dry_run_option()
@yes_option()
@click.pass_context
def clean(
    ctx: click.Context,
    *,
    directory: Path,
    exclude_patterns: tuple[str, ...],
    dry_run: bool,
    yes: bool,
) -> None:
    """Remove empty directories under DIRECTORY.

    \f

    Args:
        ctx: Active Click context carrying shared options.
        directory: Root directory to prune.
        exclude_patterns: Glob patterns for directories to preserve.
        dry_run: When set, report candidates without deleting anything.
        yes: When set, skip the interactive confirmation prompt.
    """
    console = console_from_context(ctx)
    candidates = find_empty_directories(directory, exclude_patterns=exclude_patterns)

    if not candidates:
        console.print("No empty directories found.")
        return

    if dry_run:
        for candidate in candidates:
            console.print(f"Would remove: {candidate}")
        console.print(
            f"{len(candidates)} empty director"
            f"{'y' if len(candidates) == 1 else 'ies'} to remove (dry run).",
        )
        return

    if not yes and not click.confirm(
        f"Remove {len(candidates)} empty director"
        f"{'y' if len(candidates) == 1 else 'ies'}?",
    ):
        console.print("Aborted.")
        return

    removed = remove_empty_directories(candidates)
    console.print(
        f"Removed {len(removed)} empty director{'y' if len(removed) == 1 else 'ies'}.",
    )
