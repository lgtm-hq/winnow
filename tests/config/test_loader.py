"""Tests for Dynaconf-backed configuration loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
from assertpy import assert_that

from winnow.config import (
    CONFIG_FILE_NAME,
    generate_default_config,
    load_config,
    reset_config,
    set_config_value,
    show_config,
)
from winnow.exceptions import ConfigError
from winnow.models.enums import HashAlgorithm, SymlinkPolicy


def test_load_config_returns_defaults_without_file(tmp_path: Path) -> None:
    """Verify missing config files fall back to validated defaults."""
    config = load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that(config.dry_run).is_true()
    assert_that(config.workers).is_equal_to(1)
    assert_that(config.cache.enabled).is_true()


def test_load_config_reads_working_directory_file(tmp_path: Path) -> None:
    """Verify the working-directory config file is loaded."""
    config_path = tmp_path / CONFIG_FILE_NAME
    cache_dir = tmp_path / "winnow-cache"
    output_dir = tmp_path / "winnow-output"
    config_path.write_text(
        "\n".join(
            [
                "hash_algorithm: phash",
                "dry_run: false",
                "workers: 4",
                "cache:",
                "  enabled: false",
                f"  directory: {cache_dir}",
                "paths:",
                f"  output_dir: {output_dir}",
            ],
        ),
        encoding="utf-8",
    )

    config = load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that(config.hash_algorithm).is_equal_to(HashAlgorithm.PHASH)
    assert_that(config.dry_run).is_false()
    assert_that(config.workers).is_equal_to(4)
    assert_that(config.cache.enabled).is_false()
    assert_that(config.paths.output_dir).is_equal_to(output_dir)


def test_load_config_prefers_working_directory_over_user_file(
    tmp_path: Path,
) -> None:
    """Verify CWD config has precedence over the user config file."""
    user_dir = tmp_path / "home"
    user_dir.mkdir()
    (user_dir / CONFIG_FILE_NAME).write_text(
        "dry_run: false\nworkers: 2\n",
        encoding="utf-8",
    )
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "dry_run: true\nworkers: 5\n",
        encoding="utf-8",
    )

    config = load_config(cwd=tmp_path, home_config_dir=user_dir)

    assert_that(config.dry_run).is_true()
    assert_that(config.workers).is_equal_to(5)


def test_load_config_applies_environment_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify WINNOW-prefixed environment variables override file settings."""
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "dry_run: false\nworkers: 2\ncache:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WINNOW_DRY_RUN", "true")
    monkeypatch.setenv("WINNOW_WORKERS", "6")
    monkeypatch.setenv("WINNOW_CACHE__ENABLED", "false")
    monkeypatch.setenv("DYNACONF_WORKERS", "8")

    config = load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that(config.dry_run).is_true()
    assert_that(config.workers).is_equal_to(6)
    assert_that(config.cache.enabled).is_false()


def test_load_config_syncs_follow_symlinks_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify WINNOW_FOLLOW_SYMLINKS alone derives the matching policy."""
    monkeypatch.setenv("WINNOW_FOLLOW_SYMLINKS", "true")

    config = load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that(config.follow_symlinks).is_true()
    assert_that(config.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)


def test_load_config_syncs_symlink_policy_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify WINNOW_SYMLINK_POLICY alone derives the matching follow flag."""
    monkeypatch.setenv("WINNOW_SYMLINK_POLICY", "follow")

    config = load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that(config.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)
    assert_that(config.follow_symlinks).is_true()


def test_load_config_preserves_explicit_symlink_environment_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify explicit conflicting symlink env overrides remain invalid."""
    monkeypatch.setenv("WINNOW_FOLLOW_SYMLINKS", "true")
    monkeypatch.setenv("WINNOW_SYMLINK_POLICY", "skip")

    with pytest.raises(ConfigError) as error:
        load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that(str(error.value)).contains("validate_config")


def test_load_config_does_not_mutate_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify config loading leaves matching environment variables untouched."""
    monkeypatch.setenv("WINNOW_WORKERS", "6")
    monkeypatch.setenv("DYNACONF_WORKERS", "8")

    config = load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that(config.workers).is_equal_to(6)
    assert_that(os.environ["WINNOW_WORKERS"]).is_equal_to("6")
    assert_that(os.environ["DYNACONF_WORKERS"]).is_equal_to("8")


def test_load_config_can_disable_environment_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify disabled environment loading ignores Dynaconf defaults."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 2\n", encoding="utf-8")
    monkeypatch.setenv("WINNOW_WORKERS", "6")
    monkeypatch.setenv("DYNACONF_WORKERS", "8")

    config = load_config(config_path=config_path, load_env=False)

    assert_that(config.workers).is_equal_to(2)


def test_load_config_wraps_invalid_config_errors(tmp_path: Path) -> None:
    """Verify invalid configuration raises ConfigError."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("min_similarity: 2.0\n", encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_config(config_path=config_path)

    assert_that(str(error.value)).contains("validate_config")


def test_generate_default_config_writes_first_run_file(tmp_path: Path) -> None:
    """Verify default config generation writes a loadable YAML file."""
    config_path = generate_default_config(cwd=tmp_path)

    config = load_config(config_path=config_path)

    assert_that(config_path).is_equal_to(tmp_path / CONFIG_FILE_NAME)
    assert_that(config.dry_run).is_true()
    assert_that(config.cache.enabled).is_true()


def test_set_config_value_and_reset_config_persist_changes(tmp_path: Path) -> None:
    """Verify programmatic set and reset operations update the YAML file."""
    config_path = tmp_path / CONFIG_FILE_NAME
    generate_default_config(config_path=config_path)

    updated_config = set_config_value(
        "cache.enabled",
        False,
        config_path=config_path,
    )
    shown_config = show_config(updated_config)
    shown_cache = cast("dict[str, object]", shown_config["cache"])
    reloaded_config = load_config(config_path=config_path)

    assert_that(updated_config.cache.enabled).is_false()
    assert_that(shown_cache["enabled"]).is_false()
    assert_that(reloaded_config.cache.enabled).is_false()

    reset = reset_config(config_path=config_path)
    reset_reloaded = load_config(config_path=config_path)

    assert_that(reset.cache.enabled).is_true()
    assert_that(reset_reloaded.cache.enabled).is_true()


def test_set_config_value_ignores_dynaconf_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify persisting a key does not merge unrelated DYNACONF variables."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 2\n", encoding="utf-8")
    monkeypatch.setenv("DYNACONF_WORKERS", "8")

    updated_config = set_config_value("dry_run", False, config_path=config_path)
    reloaded_config = load_config(config_path=config_path, load_env=False)

    assert_that(updated_config.workers).is_equal_to(2)
    assert_that(reloaded_config.workers).is_equal_to(2)


def test_set_config_value_updates_symlink_policy_with_follow_flag(
    tmp_path: Path,
) -> None:
    """Verify setting follow_symlinks also persists the matching policy."""
    config_path = tmp_path / CONFIG_FILE_NAME
    generate_default_config(config_path=config_path)

    followed_config = set_config_value(
        "follow_symlinks",
        True,
        config_path=config_path,
    )
    followed_reload = load_config(config_path=config_path, load_env=False)
    skipped_config = set_config_value(
        "follow_symlinks",
        False,
        config_path=config_path,
    )
    skipped_reload = load_config(config_path=config_path, load_env=False)

    assert_that(followed_config.follow_symlinks).is_true()
    assert_that(followed_config.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)
    assert_that(followed_reload.follow_symlinks).is_true()
    assert_that(followed_reload.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)
    assert_that(skipped_config.follow_symlinks).is_false()
    assert_that(skipped_config.symlink_policy).is_equal_to(SymlinkPolicy.SKIP)
    assert_that(skipped_reload.follow_symlinks).is_false()
    assert_that(skipped_reload.symlink_policy).is_equal_to(SymlinkPolicy.SKIP)


@pytest.mark.parametrize(
    "policy",
    [SymlinkPolicy.ERROR, SymlinkPolicy.SKIP],
)
def test_set_config_value_preserves_compatible_non_following_policy(
    tmp_path: Path,
    policy: SymlinkPolicy,
) -> None:
    """Verify disabling symlink following preserves compatible policies."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text(
        "\n".join(
            [
                "follow_symlinks: false",
                f"symlink_policy: {policy.value}",
            ],
        ),
        encoding="utf-8",
    )

    updated_config = set_config_value(
        "follow_symlinks",
        False,
        config_path=config_path,
    )
    reloaded_config = load_config(config_path=config_path, load_env=False)

    assert_that(updated_config.follow_symlinks).is_false()
    assert_that(updated_config.symlink_policy).is_equal_to(policy)
    assert_that(reloaded_config.follow_symlinks).is_false()
    assert_that(reloaded_config.symlink_policy).is_equal_to(policy)


def test_set_config_value_updates_follow_flag_with_symlink_policy(
    tmp_path: Path,
) -> None:
    """Verify setting symlink_policy also persists the matching follow flag."""
    config_path = tmp_path / CONFIG_FILE_NAME
    generate_default_config(config_path=config_path)

    followed_config = set_config_value(
        "symlink_policy",
        "follow",
        config_path=config_path,
    )
    followed_reload = load_config(config_path=config_path, load_env=False)
    error_config = set_config_value(
        "symlink_policy",
        SymlinkPolicy.ERROR,
        config_path=config_path,
    )
    error_reload = load_config(config_path=config_path, load_env=False)

    assert_that(followed_config.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)
    assert_that(followed_config.follow_symlinks).is_true()
    assert_that(followed_reload.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)
    assert_that(followed_reload.follow_symlinks).is_true()
    assert_that(error_config.symlink_policy).is_equal_to(SymlinkPolicy.ERROR)
    assert_that(error_config.follow_symlinks).is_false()
    assert_that(error_reload.symlink_policy).is_equal_to(SymlinkPolicy.ERROR)
    assert_that(error_reload.follow_symlinks).is_false()
