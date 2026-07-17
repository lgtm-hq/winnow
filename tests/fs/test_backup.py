"""Tests for filesystem backup helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.fs import (
    BackupOptions,
    FileSystemOperationError,
    create_backup,
    restore_backup,
)
from winnow.fs import backup as backup_module
from winnow.fs._path_ops import remove_path as remove_fs_path


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


def test_create_backup_rejects_nested_backup_directory(tmp_path: Path) -> None:
    """A backup directory inside the source directory is rejected."""
    source = tmp_path / "library"
    source.mkdir()
    (source / "photo.jpg").write_text("data\n", encoding="utf-8")

    with pytest.raises(FileSystemOperationError):
        create_backup(
            source,
            options=BackupOptions(directory=source / ".winnow-backups"),
        )


def test_create_backup_removes_partial_backup_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed copy leaves no partially written backup in the backup directory."""
    source = tmp_path / "settings.yaml"
    backup_directory = tmp_path / "backups"
    source.write_text("version: 1\n", encoding="utf-8")

    def fail_copy(*, source: Path, destination: Path) -> None:
        """Write partial staging content then raise a deterministic failure."""
        del source
        destination.write_text("partial\n", encoding="utf-8")
        raise OSError("copy failed")

    monkeypatch.setattr(backup_module, "copy_path", fail_copy)

    with pytest.raises(OSError, match="copy failed"):
        create_backup(source, options=BackupOptions(directory=backup_directory))

    assert_that(list(backup_directory.iterdir())).is_empty()


def test_create_backup_preserves_copy_error_when_partial_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed partial cleanup is attached without masking the copy failure."""
    source = tmp_path / "settings.yaml"
    backup_directory = tmp_path / "backups"
    source.write_text("version: 1\n", encoding="utf-8")

    def fail_copy(*, source: Path, destination: Path) -> None:
        """Write partial staging content then raise a deterministic failure."""
        del source
        destination.write_text("partial\n", encoding="utf-8")
        raise OSError("copy failed")

    def fail_remove(path: Path) -> None:
        """Raise a deterministic cleanup failure for monkeypatching."""
        del path
        raise OSError("cleanup failed")

    monkeypatch.setattr(backup_module, "copy_path", fail_copy)
    monkeypatch.setattr(backup_module, "remove_path", fail_remove)

    with pytest.raises(OSError, match="copy failed") as exc_info:
        create_backup(source, options=BackupOptions(directory=backup_directory))

    assert_that(getattr(exc_info.value, "__notes__", [])).contains(
        "cleanup failed: cleanup failed",
    )


def test_restore_backup_reports_tombstone_cleanup_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-restore tombstone cleanup failures report the restore was applied."""
    backup_path = tmp_path / "settings.yaml.bak"
    destination = tmp_path / "settings.yaml"
    backup_path.write_text("restored\n", encoding="utf-8")
    destination.write_text("current\n", encoding="utf-8")

    def fail_tombstone_remove(path: Path) -> None:
        """Fail only when removing the restore tombstone."""
        if path.name.startswith(".settings.yaml."):
            raise OSError("tombstone cleanup failed")
        remove_fs_path(path)

    monkeypatch.setattr(backup_module, "remove_path", fail_tombstone_remove)

    with pytest.raises(FileSystemOperationError) as exc_info:
        restore_backup(backup_path=backup_path, destination=destination)

    error = exc_info.value
    tombstones = [
        path for path in tmp_path.iterdir() if path.name.startswith(".settings.yaml.")
    ]
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("restored\n")
    assert_that(tombstones).is_length(1)
    assert_that(tombstones[0].read_text(encoding="utf-8")).is_equal_to("current\n")
    assert_that(error.__cause__).is_instance_of(OSError)
    assert_that(error.context.operation).is_equal_to("fs.restore.cleanup")
    assert_that(error.context.details).contains_entry({"restored": True})
    assert_that(error.context.details).contains_entry({"tombstone_remains": True})


def test_restore_backup_preserves_destination_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed restore preserves the existing destination contents."""
    backup_path = tmp_path / "settings.yaml.bak"
    destination = tmp_path / "settings.yaml"
    backup_path.write_text("restored\n", encoding="utf-8")
    destination.write_text("current\n", encoding="utf-8")

    def fail_copy(*, source: Path, destination: Path) -> None:
        """Raise a deterministic copy failure for monkeypatching."""
        del source, destination
        raise OSError("copy failed")

    monkeypatch.setattr(backup_module, "copy_path", fail_copy)

    with pytest.raises(FileSystemOperationError):
        restore_backup(backup_path=backup_path, destination=destination)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("current\n")
    assert_that([path.name for path in tmp_path.iterdir()]).contains_only(
        "settings.yaml.bak",
        "settings.yaml",
    )


def test_restore_backup_removes_created_parents_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed restore removes only parent directories it created."""
    backup_path = tmp_path / "settings.yaml.bak"
    destination = tmp_path / "created" / "nested" / "settings.yaml"
    backup_path.write_text("restored\n", encoding="utf-8")

    def fail_copy(*, source: Path, destination: Path) -> None:
        """Raise a deterministic copy failure for monkeypatching."""
        del source, destination
        raise OSError("copy failed")

    monkeypatch.setattr(backup_module, "copy_path", fail_copy)

    with pytest.raises(FileSystemOperationError):
        restore_backup(backup_path=backup_path, destination=destination)

    assert_that((tmp_path / "created").exists()).is_false()
    assert_that(list(tmp_path.iterdir())).contains_only(backup_path)


def test_restore_backup_restores_tombstone_when_promotion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure while promoting the staged copy restores the tombstoned file."""
    backup_path = tmp_path / "settings.yaml.bak"
    destination = tmp_path / "settings.yaml"
    backup_path.write_text("restored\n", encoding="utf-8")
    destination.write_text("current\n", encoding="utf-8")

    original_replace = Path.replace
    state = {"failed": False}

    def failing_replace(self: Path, target: str | Path) -> Path:
        """Fail once while promoting the staged copy onto the destination."""
        if Path(target).name == "settings.yaml" and not state["failed"]:
            state["failed"] = True
            raise OSError("replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(FileSystemOperationError):
        restore_backup(backup_path=backup_path, destination=destination)

    monkeypatch.setattr(Path, "replace", original_replace)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("current\n")
    assert_that([path.name for path in tmp_path.iterdir()]).contains_only(
        "settings.yaml.bak",
        "settings.yaml",
    )


def test_restore_backup_restores_tombstone_after_staging_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tombstone restoration still runs after staging cleanup fails."""
    backup_path = tmp_path / "settings.yaml.bak"
    destination = tmp_path / "settings.yaml"
    backup_path.write_text("restored\n", encoding="utf-8")
    destination.write_text("current\n", encoding="utf-8")

    original_replace = Path.replace

    def failing_replace(self: Path, target: str | Path) -> Path:
        """Fail once while promoting the restored staged file."""
        if (
            Path(target) == destination
            and self.read_text(encoding="utf-8") == "restored\n"
        ):
            raise OSError("replace failed")
        return original_replace(self, target)

    def fail_staging_remove(path: Path) -> None:
        """Fail only when removing restore staging paths."""
        if path.name.startswith(".settings.yaml."):
            raise OSError("cleanup failed")
        remove_fs_path(path)

    monkeypatch.setattr(Path, "replace", failing_replace)
    monkeypatch.setattr(backup_module, "remove_path", fail_staging_remove)

    with pytest.raises(FileSystemOperationError) as exc_info:
        restore_backup(backup_path=backup_path, destination=destination)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("current\n")
    assert_that(getattr(exc_info.value, "__notes__", [])).contains(
        "cleanup failed: cleanup failed",
    )
