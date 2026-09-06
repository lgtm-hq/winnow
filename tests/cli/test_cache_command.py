"""Tests for the ``winnow cache`` command group."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner, Result

from winnow.cli import main
from winnow.hash import CacheKey, HashCache
from winnow.models.enums import HashAlgorithm


@dataclass(frozen=True)
class SeededCache:
    """A temporary config file pointing at a cache seeded with two entries.

    Attributes:
        config_path: Configuration file whose ``cache.directory`` is temporary.
        db_path: The ``cache.db`` file under that directory.
        stale_path: The cached path whose file has been deleted.
    """

    config_path: Path
    db_path: Path
    stale_path: Path


def _entry_count(db_path: Path) -> int:
    """Return the number of rows in an on-disk cache database.

    Args:
        db_path: Location of the cache database.

    Returns:
        The current ``entry_count`` reported by the cache.
    """
    with HashCache(db_path=db_path) as hash_cache:
        return hash_cache.stats().entry_count


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Write a config file that places the cache under ``tmp_path``.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written configuration file.
    """
    path = tmp_path / "winnow.yaml"
    cache_dir = tmp_path / "cache"
    path.write_text(f"cache:\n  directory: {cache_dir}\n", encoding="utf-8")
    return path


@pytest.fixture
def seeded(tmp_path: Path, config_path: Path) -> SeededCache:
    """Seed the configured cache with one live and one stale entry.

    Args:
        tmp_path: Pytest-provided temporary directory.
        config_path: Configuration file pointing at the temporary cache.

    Returns:
        Handles for the config, database, and deleted media path.
    """
    kept = tmp_path / "kept.jpg"
    removed = tmp_path / "removed.jpg"
    kept.write_bytes(b"kept")
    removed.write_bytes(b"removed")
    db_path = tmp_path / "cache" / "cache.db"
    with HashCache(db_path=db_path) as hash_cache:
        hash_cache.set(
            key=CacheKey.from_file(path=kept, algorithm=HashAlgorithm.PHASH),
            digest="kept",
        )
        hash_cache.set(
            key=CacheKey.from_file(path=removed, algorithm=HashAlgorithm.PHASH),
            digest="removed",
        )
    removed.unlink()
    return SeededCache(
        config_path=config_path,
        db_path=db_path,
        stale_path=removed.resolve(),
    )


def _invoke(config_path: Path, *args: str, input: str | None = None) -> Result:
    """Run ``winnow cache`` against an explicit configuration file.

    Args:
        config_path: Configuration file passed via ``--config``.
        *args: Subcommand and flags following ``cache``.
        input: Text fed to stdin for confirmation prompts.

    Returns:
        The Click test result.
    """
    return CliRunner().invoke(
        main,
        ["cache", *args, "--config", str(config_path)],
        input=input,
    )


@pytest.mark.parametrize("args", [[], ["show"], ["clear"], ["prune"]])
def test_cache_help_hides_args_section(args: list[str]) -> None:
    """Every ``--help`` output stops before the ``Args:`` docstring section."""
    result = CliRunner().invoke(main, ["cache", *args, "--help"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).does_not_contain("Args:")


def test_cache_help_lists_subcommands() -> None:
    """``winnow cache --help`` lists show, clear, and prune."""
    result = CliRunner().invoke(main, ["cache", "--help"])

    assert_that(result.output).contains("show", "clear", "prune")


def test_show_renders_hash_row_and_footer(seeded: SeededCache) -> None:
    """``cache show`` prints the hash row with the seeded count and the footer."""
    result = _invoke(seeded.config_path, "show")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Cache", "hash", "2")
    assert_that(result.output).contains(f"Database: {seeded.db_path} (")


def test_show_without_database_reports_zeros(config_path: Path) -> None:
    """``cache show`` on a missing database prints zeros and the notice."""
    result = _invoke(config_path, "show")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("hash", "0", "no cache database at")


def test_clear_dry_run_changes_nothing(seeded: SeededCache) -> None:
    """``cache clear --dry-run`` previews the count and keeps every entry."""
    result = _invoke(seeded.config_path, "clear", "--dry-run")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Would remove 2 entries.")
    assert_that(_entry_count(seeded.db_path)).is_equal_to(2)


def test_clear_declined_prompt_aborts(seeded: SeededCache) -> None:
    """Answering ``n`` to the clear prompt prints Aborted. and keeps entries."""
    result = _invoke(seeded.config_path, "clear", input="n\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Remove 2 cached entries?", "Aborted.")
    assert_that(_entry_count(seeded.db_path)).is_equal_to(2)


def test_clear_with_yes_empties_cache(seeded: SeededCache) -> None:
    """``cache clear --yes`` removes every entry without prompting."""
    result = _invoke(seeded.config_path, "clear", "--yes")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Removed 2 entries.")
    assert_that(result.output).does_not_contain("?")
    assert_that(_entry_count(seeded.db_path)).is_equal_to(0)


def test_clear_empty_cache_reports_nothing_to_remove(seeded: SeededCache) -> None:
    """``cache clear`` on an empty database exits 0 without prompting."""
    _invoke(seeded.config_path, "clear", "--yes")

    result = _invoke(seeded.config_path, "clear")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Nothing to remove.")


def test_prune_dry_run_lists_stale_path(seeded: SeededCache) -> None:
    """``cache prune --dry-run`` lists the stale path and deletes nothing."""
    result = _invoke(seeded.config_path, "prune", "--dry-run")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains(
        str(seeded.stale_path),
        "1 stale entries (dry run).",
    )
    assert_that(_entry_count(seeded.db_path)).is_equal_to(2)


def test_prune_declined_prompt_aborts(seeded: SeededCache) -> None:
    """Answering ``n`` to the prune prompt prints Aborted. and keeps entries."""
    result = _invoke(seeded.config_path, "prune", input="n\n")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Prune 1 stale entries?", "Aborted.")
    assert_that(_entry_count(seeded.db_path)).is_equal_to(2)


def test_prune_with_yes_removes_only_stale_entry(seeded: SeededCache) -> None:
    """``cache prune --yes`` drops the stale row and keeps the live one."""
    result = _invoke(seeded.config_path, "prune", "--yes")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("Pruned 1 entries.")
    assert_that(_entry_count(seeded.db_path)).is_equal_to(1)
    with HashCache(db_path=seeded.db_path) as hash_cache:
        assert_that(hash_cache.stale_paths()).is_empty()


def test_prune_without_stale_entries_reports_none(seeded: SeededCache) -> None:
    """``cache prune`` after pruning reports no stale entries and exits 0."""
    _invoke(seeded.config_path, "prune", "--yes")

    result = _invoke(seeded.config_path, "prune")

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("No stale entries.")


@pytest.mark.parametrize("subcommand", ["clear", "prune"])
def test_destructive_commands_without_database_exit_zero(
    config_path: Path,
    subcommand: str,
) -> None:
    """Missing database: clear and prune report it, exit 0, and create nothing."""
    result = _invoke(config_path, subcommand)

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("no cache database at")
    assert_that((config_path.parent / "cache" / "cache.db").exists()).is_false()


def test_cache_invalid_config_exits_with_failure(tmp_path: Path) -> None:
    """A broken configuration file surfaces as the standard exit code 1."""
    config_path = tmp_path / "winnow.yaml"
    config_path.write_text("workers: not-a-number\n", encoding="utf-8")

    result = _invoke(config_path, "show")

    assert_that(result.exit_code).is_equal_to(1)
