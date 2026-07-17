"""Tests for transactional atomic filesystem operations."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.fs import (
    BackupOptions,
    FileOperation,
    FileSystemOperationError,
    OperationLog,
    OperationStatus,
    atomic_copy,
    atomic_mkdir,
    transactional_file_ops,
)


def test_atomic_copy_overwrites_destination_and_records_backup(
    tmp_path: Path,
) -> None:
    """Atomic copy replaces a file and stores a backup of overwritten content."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    backup_directory = tmp_path / "backups"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    log = atomic_copy(
        source=source,
        destination=destination,
        backup=BackupOptions(directory=backup_directory),
    )

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("new\n")
    assert_that(source.read_text(encoding="utf-8")).is_equal_to("new\n")
    assert_that(log.operation).is_equal_to(FileOperation.COPY)
    assert_that(log.backups).is_length(1)
    assert_that(log.backups[0].read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that(log.as_dict()).contains_key("backups")


def test_atomic_copy_failure_leaves_destination_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy failure before replacement leaves source and destination unchanged."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    def fail_copy2(
        src: str | Path,
        dst: str | Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        """Raise a deterministic copy failure for monkeypatching."""
        del src, dst, follow_symlinks
        raise OSError("copy failed")

    monkeypatch.setattr(shutil, "copy2", fail_copy2)

    with pytest.raises(FileSystemOperationError):
        atomic_copy(source=source, destination=destination)

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("new\n")
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that([path.name for path in tmp_path.iterdir()]).contains_only(
        "source.txt",
        "destination.txt",
    )


def test_transaction_rolls_back_delete_and_preserves_backup(
    tmp_path: Path,
) -> None:
    """A failed transaction restores a deleted file and keeps its backup."""
    target = tmp_path / "settings.yaml"
    backup_directory = tmp_path / "backups"
    target.write_text("dry_run: true\n", encoding="utf-8")
    transaction = transactional_file_ops(
        backup=BackupOptions(directory=backup_directory),
    )
    delete_log: OperationLog | None = None

    with pytest.raises(RuntimeError):
        with transaction as active_transaction:
            delete_log = active_transaction.delete(target)
            assert_that(target.exists()).is_false()
            raise RuntimeError("abort batch")

    assert_that(target.read_text(encoding="utf-8")).is_equal_to("dry_run: true\n")
    assert_that(transaction.rollback_errors).is_empty()
    assert_that(transaction.logs).is_length(1)
    assert_that(transaction.logs[0].status).is_equal_to(OperationStatus.ROLLED_BACK)
    assert_that(delete_log).is_not_none()
    if delete_log is None:
        return
    assert_that(delete_log.backups).is_length(1)
    assert_that(delete_log.backups[0].read_text(encoding="utf-8")).is_equal_to(
        "dry_run: true\n",
    )


def test_transaction_rolls_back_move_over_existing_destination(
    tmp_path: Path,
) -> None:
    """A failed transaction restores source and overwritten destination files."""
    source = tmp_path / "candidate.jpg"
    destination = tmp_path / "library.jpg"
    source.write_text("candidate\n", encoding="utf-8")
    destination.write_text("library\n", encoding="utf-8")
    transaction = transactional_file_ops(backup=False)

    with pytest.raises(RuntimeError):
        with transaction as active_transaction:
            active_transaction.move(source=source, destination=destination)
            assert_that(source.exists()).is_false()
            assert_that(destination.read_text(encoding="utf-8")).is_equal_to(
                "candidate\n",
            )
            raise RuntimeError("abort batch")

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("candidate\n")
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("library\n")
    assert_that(transaction.logs[0].status).is_equal_to(OperationStatus.ROLLED_BACK)


def test_transaction_commit_keeps_batched_copy_in_created_directory(
    tmp_path: Path,
) -> None:
    """A successful transaction commits mkdir and copy operations."""
    source = tmp_path / "source.txt"
    destination_directory = tmp_path / "organized"
    destination = destination_directory / "source.txt"
    source.write_text("content\n", encoding="utf-8")

    with transactional_file_ops(backup=False) as transaction:
        transaction.mkdir(destination_directory)
        transaction.copy(source=source, destination=destination)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("content\n")
    assert_that(source.read_text(encoding="utf-8")).is_equal_to("content\n")


def test_transaction_rolls_back_created_parent_directories(
    tmp_path: Path,
) -> None:
    """A failed transaction removes directories created by atomic mkdir."""
    created_directory = tmp_path / "a" / "b" / "c"
    transaction = transactional_file_ops(backup=False)

    with pytest.raises(RuntimeError):
        with transaction as active_transaction:
            active_transaction.mkdir(created_directory)
            assert_that(created_directory.is_dir()).is_true()
            raise RuntimeError("abort batch")

    assert_that((tmp_path / "a").exists()).is_false()
    assert_that(transaction.logs[0].created_paths).contains(
        tmp_path / "a",
        tmp_path / "a" / "b",
        created_directory,
    )


def test_atomic_mkdir_existing_directory_with_exist_ok_records_log(
    tmp_path: Path,
) -> None:
    """Existing directories can be accepted without creating rollback work."""
    existing_directory = tmp_path / "existing"
    existing_directory.mkdir()

    log = atomic_mkdir(existing_directory, exist_ok=True, backup=False)

    assert_that(log.operation).is_equal_to(FileOperation.MKDIR)
    assert_that(log.destination).is_equal_to(existing_directory)
    assert_that(existing_directory.is_dir()).is_true()
