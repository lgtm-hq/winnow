"""Tests for filesystem backup helpers."""

from __future__ import annotations

from pathlib import Path

from assertpy import assert_that

from winnow.fs import BackupOptions, create_backup, restore_backup


def test_create_backup_and_restore_file(tmp_path: Path) -> None:
    """Backup helpers copy and restore file contents."""
    source = tmp_path / "settings.yaml"
    backup_directory = tmp_path / "backups"
    source.write_text("version: 1\n", encoding="utf-8")

    backup_path = create_backup(
        source,
        options=BackupOptions(directory=backup_directory),
    )
    source.write_text("version: 2\n", encoding="utf-8")

    assert_that(backup_path).is_not_none()
    if backup_path is None:
        return
    assert_that(backup_path.parent).is_equal_to(backup_directory)
    assert_that(backup_path.read_text(encoding="utf-8")).is_equal_to("version: 1\n")

    restore_backup(backup_path=backup_path, destination=source)

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("version: 1\n")


def test_create_backup_can_be_disabled(tmp_path: Path) -> None:
    """Disabled backup options leave the filesystem untouched."""
    source = tmp_path / "settings.yaml"
    source.write_text("version: 1\n", encoding="utf-8")

    backup_path = create_backup(
        source,
        options=BackupOptions(enabled=False),
    )

    assert_that(backup_path).is_none()
    assert_that(list(tmp_path.iterdir())).contains_only(source)


def test_restore_backup_replaces_directory(tmp_path: Path) -> None:
    """Directory backups restore nested contents over an existing directory."""
    source = tmp_path / "profile"
    backup_directory = tmp_path / "backups"
    source.mkdir()
    (source / "config.yaml").write_text("theme: dark\n", encoding="utf-8")
    backup_path = create_backup(
        source,
        options=BackupOptions(directory=backup_directory),
    )
    (source / "config.yaml").write_text("theme: light\n", encoding="utf-8")

    assert_that(backup_path).is_not_none()
    if backup_path is None:
        return
    restore_backup(backup_path=backup_path, destination=source)

    assert_that((source / "config.yaml").read_text(encoding="utf-8")).is_equal_to(
        "theme: dark\n",
    )
