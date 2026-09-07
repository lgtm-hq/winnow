"""Tests for the ``winnow prune`` command group."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.config import CONFIG_FILE_NAME
from winnow.fs.retention import BACKUP_DIRNAME

_DAY_SECONDS = 86400


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Write a config file with a 30-day backup retention.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the config file.
    """
    path = tmp_path / CONFIG_FILE_NAME
    path.write_text("retention:\n  backup_max_age_days: 30\n", encoding="utf-8")
    return path


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a library with one 40-day-old backup and one fresh backup.

    ``Path.stat`` is patched so the stale file reports a backdated ``st_ctime``.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        ``(root, stale_backup)`` paths.
    """
    root = tmp_path / "library"
    backup_dir = root / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True)
    stale = backup_dir / "old.jpg.0.bak"
    stale.write_bytes(b"x" * 2048)
    (backup_dir / "new.jpg.1.bak").write_bytes(b"y")
    real_stat = Path.stat

    def fake_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        """Backdate ``st_ctime`` for the stale backup only."""
        result = real_stat(self, follow_symlinks=follow_symlinks)
        if self != stale:
            return result
        fields = list(result)
        fields[9] = result.st_ctime - 40 * _DAY_SECONDS
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", fake_stat)
    return root, stale


def test_prune_backups_dry_run_lists_without_removing(
    library: tuple[Path, Path],
    config_path: Path,
) -> None:
    """A dry run lists the stale backup and leaves it in place."""
    root, stale = library

    result = CliRunner().invoke(
        main,
        ["prune", "backups", str(root), "--dry-run", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains(stale.name).contains("(2.0 KiB)")
    assert_that(result.output).contains("1 stale backup files (2.0 KiB) (dry run).")
    assert_that(stale.exists()).is_true()


def test_prune_backups_declined_prompt_aborts(
    library: tuple[Path, Path],
    config_path: Path,
) -> None:
    """Answering ``n`` to the prompt removes nothing."""
    root, stale = library

    result = CliRunner().invoke(
        main,
        ["prune", "backups", str(root), "--config", str(config_path)],
        input="n\n",
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Remove 1 backup files (2.0 KiB)?")
    assert_that(result.output).contains("Aborted.")
    assert_that(stale.exists()).is_true()


def test_prune_backups_yes_removes_stale_file(
    library: tuple[Path, Path],
    config_path: Path,
) -> None:
    """``--yes`` removes the stale backup and keeps the fresh one."""
    root, stale = library

    result = CliRunner().invoke(
        main,
        ["prune", "backups", str(root), "--yes", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 1 backup files (2.0 KiB).")
    assert_that(stale.exists()).is_false()
    assert_that((root / BACKUP_DIRNAME / "new.jpg.1.bak").exists()).is_true()


def test_prune_backups_nothing_to_do(tmp_path: Path, config_path: Path) -> None:
    """A library without stale backups reports so and exits 0."""
    root = tmp_path / "library"
    (root / BACKUP_DIRNAME).mkdir(parents=True)
    (root / BACKUP_DIRNAME / "new.jpg.1.bak").write_bytes(b"y")

    result = CliRunner().invoke(
        main,
        ["prune", "backups", str(root), "--yes", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("No stale backups.")


def test_prune_backups_zero_max_age_disables_pruning(
    library: tuple[Path, Path],
    config_path: Path,
) -> None:
    """``--max-age-days 0`` never selects anything."""
    root, stale = library

    result = CliRunner().invoke(
        main,
        [
            "prune",
            "backups",
            str(root),
            "--max-age-days",
            "0",
            "--yes",
            "--config",
            str(config_path),
        ],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("No stale backups.")
    assert_that(stale.exists()).is_true()


def test_prune_backups_flag_overrides_config(
    library: tuple[Path, Path],
    config_path: Path,
) -> None:
    """A ``--max-age-days`` above the file's age overrides the config default."""
    root, stale = library

    result = CliRunner().invoke(
        main,
        [
            "prune",
            "backups",
            str(root),
            "--max-age-days",
            "60",
            "--yes",
            "--config",
            str(config_path),
        ],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("No stale backups.")
    assert_that(stale.exists()).is_true()


def test_prune_help_lists_backups() -> None:
    """``winnow prune --help`` lists the ``backups`` subcommand."""
    result = CliRunner().invoke(main, ["prune", "--help"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("backups")
    assert_that(result.output).does_not_contain("Args:")
