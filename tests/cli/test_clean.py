"""Tests for the ``winnow clean`` command."""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import click
import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.clean import clean
from winnow.cli.errors import ExitCode

_WINDOWS = sys.platform.startswith("win")


def _make_tree(root: Path) -> None:
    """Create a sample directory tree with mixed empty and populated dirs.

    Args:
        root: Directory to populate.
    """
    (root / "photos" / "2020").mkdir(parents=True)
    (root / "photos" / "2020" / "pic.jpg").write_text("data")
    (root / "empty" / "nested" / "leaf").mkdir(parents=True)
    (root / "empty2").mkdir()


def test_clean_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    """A dry run lists candidates and leaves the tree intact."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("4 empty directories to remove (dry run)")
    assert_that((tmp_path / "empty").exists()).is_true()


def test_clean_dry_run_prints_markup_like_names_verbatim(tmp_path: Path) -> None:
    """Paths that look like Rich markup are printed literally."""
    # ``/`` splits the name into two nested directories, so the leaf path
    # ends in the literal ``[bold]x[/bold]``.
    (tmp_path / "[bold]x[/bold]").mkdir(parents=True)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("[bold]x[/bold]")


def test_clean_removes_directories_with_yes_flag(tmp_path: Path) -> None:
    """The --yes flag skips confirmation and removes empty directories."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--yes"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 4 empty directories.")
    assert_that((tmp_path / "empty").exists()).is_false()


def test_clean_prompts_and_aborts_on_decline(tmp_path: Path) -> None:
    """Declining the confirmation prompt preserves all directories."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path)], input="n\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Aborted.")
    assert_that((tmp_path / "empty").exists()).is_true()


def test_clean_confirms_and_removes_on_accept(tmp_path: Path) -> None:
    """Accepting the confirmation prompt removes empty directories."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path)], input="y\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 4 empty directories.")
    assert_that((tmp_path / "empty").exists()).is_false()


def test_clean_reports_when_no_empty_directories(tmp_path: Path) -> None:
    """A tree with no empty directories reports nothing to do."""
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "pic.jpg").write_text("data")

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("No empty directories found.")


def test_clean_singular_message_for_one_directory(tmp_path: Path) -> None:
    """A single empty directory uses singular phrasing."""
    (tmp_path / "solo").mkdir()

    result = CliRunner().invoke(clean, [str(tmp_path), "--yes"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 1 empty directory.")


def test_clean_removes_only_confirmed_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory created while the prompt is open is not removed."""
    (tmp_path / "old_empty").mkdir()
    created = tmp_path / "new_empty_created_during_prompt"

    def confirm_and_create(*args: object, **kwargs: object) -> bool:
        created.mkdir()
        return True

    monkeypatch.setattr(click, "confirm", confirm_and_create)

    result = CliRunner().invoke(main, ["clean", str(tmp_path)])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 1 empty directory.")
    assert_that(created.exists()).is_true()
    assert_that((tmp_path / "old_empty").exists()).is_false()


def test_clean_skips_confirmed_directory_that_gained_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed directory that gains a file is kept, reported, and exits 0."""
    # Relative paths keep Rich from wrapping the skipped lines.
    monkeypatch.chdir(tmp_path)
    leaf = Path("empty") / "leaf"
    leaf.mkdir(parents=True)
    Path("other").mkdir()

    def confirm_and_fill(*args: object, **kwargs: object) -> bool:
        (leaf / "late.txt").write_text("data")
        return True

    monkeypatch.setattr(click, "confirm", confirm_and_fill)

    result = CliRunner().invoke(main, ["clean", "."])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output.splitlines()).is_equal_to(
        [
            "Removed 1 empty directory.",
            "Skipped 2 directories.",
            f"Skipped: {leaf} (no longer empty)",
            f"Skipped: {Path('empty')} (no longer empty)",
        ],
    )
    assert_that((leaf / "late.txt").exists()).is_true()
    assert_that(Path("other").exists()).is_false()


@pytest.mark.skipif(_WINDOWS, reason="chmod 000 has no effect on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions",
)
def test_clean_reports_unreadable_directory_before_prompt(tmp_path: Path) -> None:
    """An unreadable directory is reported before the prompt and kept."""
    locked = tmp_path / "parent" / "locked"
    locked.mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    locked.chmod(0)
    try:
        result = CliRunner().invoke(main, ["clean", str(tmp_path)], input="y\n")
    finally:
        locked.chmod(stat.S_IRWXU)

    assert_that(result.exit_code).is_equal_to(0)
    notice = "1 directory could not be read and was left alone."
    prompt = "Remove 1 empty directory?"
    assert_that(result.output).contains(notice, prompt)
    assert_that(result.output.index(notice)).is_less_than(
        result.output.index(prompt),
    )
    assert_that(result.output).contains("Removed 1 empty directory.")
    assert_that(locked.exists()).is_true()
    assert_that((tmp_path / "parent").exists()).is_true()
    assert_that((tmp_path / "plain").exists()).is_false()


def test_clean_yes_output_unchanged_for_stable_tree(tmp_path: Path) -> None:
    """With --yes on a stable tree the output is exactly the removed count."""
    _make_tree(tmp_path)

    result = CliRunner().invoke(main, ["clean", str(tmp_path), "--yes"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).is_equal_to("Removed 4 empty directories.\n")


def test_clean_dry_run_output_unchanged_for_stable_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --dry-run on a stable tree the output lists candidates and a count."""
    # Relative paths keep Rich from wrapping the candidate lines.
    monkeypatch.chdir(tmp_path)
    Path("a").mkdir()
    Path("b").mkdir()

    result = CliRunner().invoke(main, ["clean", ".", "--dry-run"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output.splitlines()).is_equal_to(
        [
            "Would remove: a",
            "Would remove: b",
            "2 empty directories to remove (dry run).",
        ],
    )


def test_clean_rejects_missing_directory(tmp_path: Path) -> None:
    """A nonexistent target directory fails validation."""
    result = CliRunner().invoke(main, ["clean", str(tmp_path / "missing")])

    assert_that(result.exit_code).is_not_equal_to(0)


@pytest.mark.skipif(_WINDOWS, reason="Windows cannot open a directory descriptor")
def test_clean_reports_failed_candidates_and_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure mid-list is reported with the rest of the outcome, exit 1."""
    # Relative paths keep Rich from wrapping the skipped lines.
    monkeypatch.chdir(tmp_path)
    Path("a").mkdir()
    Path("b").mkdir()
    real_rmdir = os.rmdir

    def denied_rmdir(path: str, *, dir_fd: int | None = None) -> None:
        if path == "a":
            raise PermissionError(errno.EACCES, "denied", path)
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", denied_rmdir)
    monkeypatch.setattr(os, "supports_dir_fd", {denied_rmdir, os.open})

    result = CliRunner().invoke(main, ["clean", ".", "--yes"])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.output.splitlines()).is_equal_to(
        [
            "Removed 1 empty directory.",
            "Skipped 1 directory.",
            "Skipped: a (permission denied)",
        ],
    )
    assert_that(Path("a").is_dir()).is_true()
    assert_that(Path("b").exists()).is_false()


@pytest.mark.skipif(_WINDOWS, reason="symlinks need privileges on Windows")
def test_clean_does_not_follow_symlink_swapped_in_during_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor swapped for a symlink never removes anything outside."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "parent" / "child").mkdir(parents=True)
    (outside / "real" / "child").mkdir(parents=True)

    def confirm_and_swap(*args: object, **kwargs: object) -> bool:
        (root / "parent" / "child").rmdir()
        (root / "parent").rmdir()
        (root / "parent").symlink_to(outside / "real", target_is_directory=True)
        return True

    monkeypatch.setattr(click, "confirm", confirm_and_swap)

    result = CliRunner().invoke(main, ["clean", str(root)])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.output).contains("Removed 0 empty directories.")
    assert_that(result.output).contains("path changed")
    assert_that((outside / "real" / "child").is_dir()).is_true()


@pytest.mark.skipif(_WINDOWS, reason="chmod 000 has no effect on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions",
)
def test_clean_does_not_report_unreadable_excluded_directory(tmp_path: Path) -> None:
    """An excluded directory is never read, so it is not reported."""
    excluded = tmp_path / ".git"
    excluded.mkdir()
    (tmp_path / "plain").mkdir()
    excluded.chmod(0)
    try:
        result = CliRunner().invoke(
            main,
            ["clean", str(tmp_path), "--exclude", ".git", "--yes"],
        )
    finally:
        excluded.chmod(stat.S_IRWXU)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).is_equal_to("Removed 1 empty directory.\n")
    assert_that(excluded.is_dir()).is_true()
