"""Tests for transactional atomic filesystem operations."""

from __future__ import annotations

import errno
import shutil
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.fs import (
    BackupOptions,
    FileOperation,
    FileSystemOperationError,
    FileSystemRollbackError,
    OperationLog,
    OperationStatus,
    atomic_copy,
    atomic_delete,
    atomic_mkdir,
    atomic_move,
    transactional_file_ops,
)
from winnow.fs import transaction as transaction_module
from winnow.fs._path_ops import copy_path as copy_path_helper


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


def test_rollback_after_commit_preserves_committed_log_status(
    tmp_path: Path,
) -> None:
    """Calling rollback after a successful commit must not rewrite log status."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("content\n", encoding="utf-8")

    transaction = transactional_file_ops(backup=False)
    with transaction as active_transaction:
        log = active_transaction.copy(source=source, destination=destination)

    assert_that(log.status).is_equal_to(OperationStatus.APPLIED)

    errors = transaction.rollback()

    assert_that(errors).is_empty()
    assert_that(log.status).is_equal_to(OperationStatus.APPLIED)
    assert_that(transaction.logs[0].status).is_equal_to(OperationStatus.APPLIED)
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("content\n")


def test_rollback_after_failed_commit_cleanup_preserves_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback after a commit cleanup failure must not undo committed work."""
    target = tmp_path / "target.txt"
    target.write_text("content\n", encoding="utf-8")

    def fail_commit_tombstones(*tombstones: Path | None) -> None:
        """Raise a deterministic commit cleanup failure for monkeypatching."""
        del tombstones
        raise OSError("commit cleanup failed")

    monkeypatch.setattr(
        transaction_module,
        "_commit_tombstones",
        fail_commit_tombstones,
    )

    transaction = transactional_file_ops(backup=False)
    log: OperationLog | None = None
    with pytest.raises(FileSystemOperationError):
        with transaction as active_transaction:
            log = active_transaction.delete(target)

    assert_that(target.exists()).is_false()
    assert_that(log).is_not_none()
    if log is None:
        return
    assert_that(log.status).is_equal_to(OperationStatus.APPLIED)

    errors = transaction.rollback()

    assert_that(errors).is_empty()
    assert_that(target.exists()).is_false()
    assert_that(log.status).is_equal_to(OperationStatus.APPLIED)
    assert_that(transaction.logs[0].status).is_equal_to(OperationStatus.APPLIED)


def test_reused_transaction_clears_prior_run_logs(
    tmp_path: Path,
) -> None:
    """Re-entering a transaction instance discards logs from the prior run."""
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_destination = tmp_path / "first-destination.txt"
    second_destination = tmp_path / "second-destination.txt"
    first_source.write_text("first\n", encoding="utf-8")
    second_source.write_text("second\n", encoding="utf-8")

    transaction = transactional_file_ops(backup=False)
    with transaction as active_transaction:
        active_transaction.copy(source=first_source, destination=first_destination)

    assert_that(transaction.logs).is_length(1)

    with transaction as active_transaction:
        active_transaction.copy(source=second_source, destination=second_destination)

    assert_that(transaction.logs).is_length(1)
    assert_that(transaction.logs[0].destination).is_equal_to(second_destination)


def test_copy_cleanup_failure_is_attached_and_original_error_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure must not mask the original filesystem operation error."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")

    def fail_copy2(
        src: str | Path,
        dst: str | Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        """Raise a deterministic copy failure for monkeypatching."""
        del src, dst, follow_symlinks
        raise OSError("copy failed")

    def fail_cleanup(path: Path) -> None:
        """Raise a deterministic cleanup failure for monkeypatching."""
        del path
        raise OSError("cleanup failed")

    monkeypatch.setattr(shutil, "copy2", fail_copy2)
    monkeypatch.setattr(transaction_module, "_cleanup_path", fail_cleanup)

    with pytest.raises(FileSystemOperationError) as exc_info:
        atomic_copy(source=source, destination=destination)

    assert_that(exc_info.value.__cause__).is_instance_of(OSError)
    notes = getattr(exc_info.value, "__notes__", [])
    assert_that(notes).is_not_empty()
    assert_that(notes[0]).contains("cleanup failed")


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


def test_transaction_rejects_nested_entry(tmp_path: Path) -> None:
    """Re-entering an already active transaction is rejected."""
    del tmp_path
    transaction = transactional_file_ops(backup=False)

    with transaction:
        with pytest.raises(FileSystemOperationError):
            with transaction:
                pass


def test_transaction_rejects_operations_after_close(tmp_path: Path) -> None:
    """Operations invoked after the context closes are rejected."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("content\n", encoding="utf-8")

    transaction = transactional_file_ops(backup=False)
    with transaction as active_transaction:
        active_transaction.copy(source=source, destination=destination)

    with pytest.raises(FileSystemOperationError):
        transaction.copy(source=source, destination=tmp_path / "other.txt")


def test_copy_failure_removes_orphaned_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed copy over an existing destination removes its created backup."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    backup_directory = tmp_path / "backups"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    original_replace = Path.replace

    def failing_replace(self: Path, target: str | Path) -> Path:
        """Fail when atomically replacing the destination."""
        if Path(target) == destination:
            raise OSError("replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(FileSystemOperationError):
        atomic_copy(
            source=source,
            destination=destination,
            backup=BackupOptions(directory=backup_directory),
        )

    monkeypatch.setattr(Path, "replace", original_replace)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that(list(backup_directory.iterdir())).is_empty()


def test_backup_cleanup_attempts_every_backup_before_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orphaned backup cleanup attempts every path before reporting failures."""
    backup_a = tmp_path / "a.txt.bak"
    backup_b = tmp_path / "b.txt.bak"
    backup_a.write_text("a\n", encoding="utf-8")
    backup_b.write_text("b\n", encoding="utf-8")
    calls: list[Path] = []

    def fail_cleanup(path: Path) -> None:
        """Record cleanup attempts and raise a deterministic failure."""
        calls.append(path)
        raise OSError("cleanup failed")

    monkeypatch.setattr(transaction_module, "_cleanup_path", fail_cleanup)

    with pytest.raises(FileSystemOperationError) as exc_info:
        transaction_module._cleanup_backups([backup_a, backup_b])

    assert_that(calls).is_equal_to([backup_a, backup_b])
    assert_that(exc_info.value.context.details["errors"]).is_length(2)


def test_run_cleanups_preserves_nested_backup_cleanup_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup aggregation notes remain attached to the primary error."""
    backup_a = tmp_path / "a.txt.bak"
    backup_b = tmp_path / "b.txt.bak"
    backup_a.write_text("a\n", encoding="utf-8")
    backup_b.write_text("b\n", encoding="utf-8")

    def fail_cleanup(path: Path) -> None:
        """Raise a path-specific cleanup failure for aggregation."""
        raise OSError(f"cleanup failed for {path.name}")

    monkeypatch.setattr(transaction_module, "_cleanup_path", fail_cleanup)
    primary_error = FileSystemOperationError(
        "failed to apply filesystem copy",
        operation="fs.copy",
    )

    transaction_module._run_cleanups(
        primary_error,
        [lambda: transaction_module._cleanup_backups([backup_a, backup_b])],
    )

    assert_that(getattr(primary_error, "__notes__", [])).contains(
        "cleanup detail: backup cleanup failed: cleanup failed for b.txt.bak",
    )


def test_commit_attempts_all_cleanups_before_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every commit cleanup runs even when an earlier cleanup fails."""
    target_a = tmp_path / "a.txt"
    target_b = tmp_path / "b.txt"
    target_a.write_text("a\n", encoding="utf-8")
    target_b.write_text("b\n", encoding="utf-8")

    calls: list[tuple[Path | None, ...]] = []

    def failing_commit(*tombstones: Path | None) -> None:
        """Record the call then raise a deterministic cleanup failure."""
        calls.append(tombstones)
        raise OSError("commit cleanup failed")

    monkeypatch.setattr(transaction_module, "_commit_tombstones", failing_commit)

    transaction = transactional_file_ops(backup=False)
    with pytest.raises(FileSystemOperationError):
        with transaction as active_transaction:
            active_transaction.delete(target_a)
            active_transaction.delete(target_b)

    assert_that(calls).is_length(2)


def test_rollback_failure_keeps_failed_log_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rollback step leaves its own log un-rolled-back."""
    source_a = tmp_path / "a.txt"
    source_b = tmp_path / "b.txt"
    destination_a = tmp_path / "dest_a.txt"
    destination_b = tmp_path / "dest_b.txt"
    source_a.write_text("a\n", encoding="utf-8")
    source_b.write_text("b\n", encoding="utf-8")

    original_cleanup = transaction_module._cleanup_path

    def selective_cleanup(path: Path) -> None:
        """Fail cleanup only for the second destination during rollback."""
        if path == destination_b:
            raise OSError("cleanup failed")
        original_cleanup(path)

    monkeypatch.setattr(transaction_module, "_cleanup_path", selective_cleanup)

    transaction = transactional_file_ops(backup=False)
    log_a: OperationLog | None = None
    log_b: OperationLog | None = None
    with pytest.raises(RuntimeError):
        with transaction as active_transaction:
            log_a = active_transaction.copy(
                source=source_a,
                destination=destination_a,
            )
            log_b = active_transaction.copy(
                source=source_b,
                destination=destination_b,
            )
            raise RuntimeError("abort batch")

    assert_that(transaction.rollback_errors).is_not_empty()
    assert_that(log_a).is_not_none()
    assert_that(log_b).is_not_none()
    if log_a is None or log_b is None:
        return
    assert_that(log_a.status).is_equal_to(OperationStatus.ROLLED_BACK)
    assert_that(log_b.status).is_equal_to(OperationStatus.APPLIED)


def test_atomic_move_renames_file_and_commits(tmp_path: Path) -> None:
    """Atomic move renames a file without leaving staging artifacts."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    log = atomic_move(source=source, destination=destination, backup=False)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("payload\n")
    assert_that(source.exists()).is_false()
    assert_that(log.operation).is_equal_to(FileOperation.MOVE)
    assert_that(log.backups).is_empty()
    assert_that(list(tmp_path.iterdir())).is_length(1)


def test_atomic_move_does_not_back_up_plain_renames(tmp_path: Path) -> None:
    """A move without an overwritten destination creates no backup copies."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    backup_directory = tmp_path / "backups"
    source.write_text("payload\n", encoding="utf-8")

    log = atomic_move(
        source=source,
        destination=destination,
        backup=BackupOptions(directory=backup_directory),
    )

    assert_that(log.backups).is_empty()
    assert_that(backup_directory.exists()).is_false()


def test_atomic_move_backs_up_only_overwritten_destination(tmp_path: Path) -> None:
    """A move over an existing destination backs up just the destroyed file."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    backup_directory = tmp_path / "backups"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    log = atomic_move(
        source=source,
        destination=destination,
        backup=BackupOptions(directory=backup_directory),
    )

    assert_that(log.backups).is_length(1)
    assert_that(log.backups[0].read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("new\n")


def test_atomic_delete_removes_path_and_keeps_backup(tmp_path: Path) -> None:
    """Atomic delete removes the file and records the configured backup."""
    target = tmp_path / "target.txt"
    backup_directory = tmp_path / "backups"
    target.write_text("payload\n", encoding="utf-8")

    log = atomic_delete(target, backup=BackupOptions(directory=backup_directory))

    assert_that(target.exists()).is_false()
    assert_that(log.operation).is_equal_to(FileOperation.DELETE)
    assert_that(log.backups).is_length(1)
    assert_that(log.backups[0].read_text(encoding="utf-8")).is_equal_to("payload\n")


def test_move_falls_back_to_copy_on_cross_device_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-filesystem move copies then removes the source atomically."""
    source = tmp_path / "source.txt"
    destination_dir = tmp_path / "dest"
    destination_dir.mkdir()
    destination = destination_dir / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    original_replace = Path.replace

    def cross_device_replace(self: Path, target: str | Path) -> Path:
        """Simulate EXDEV when renaming the source into the staging path."""
        if self == source and Path(target).parent == destination_dir:
            raise OSError(errno.EXDEV, "cross-device link")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", cross_device_replace)

    log = atomic_move(source=source, destination=destination, backup=False)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("payload\n")
    assert_that(source.exists()).is_false()
    assert_that(log.operation).is_equal_to(FileOperation.MOVE)
    assert_that(list(destination_dir.iterdir())).is_length(1)
    assert_that(list(tmp_path.iterdir())).is_length(1)


def test_move_cross_device_failure_rolls_back_source_and_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cross-device move restores both source and destination."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    original_replace = Path.replace

    def cross_device_then_failing_replace(self: Path, target: str | Path) -> Path:
        """Simulate EXDEV for the source, then fail the final replace."""
        if self == source and str(Path(target).name).endswith(".tmp"):
            raise OSError(errno.EXDEV, "cross-device link")
        if Path(target) == destination:
            raise OSError("replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", cross_device_then_failing_replace)

    with pytest.raises(FileSystemOperationError):
        atomic_move(source=source, destination=destination, backup=False)

    monkeypatch.setattr(Path, "replace", original_replace)

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("new\n")
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that(list(tmp_path.iterdir())).is_length(2)


def test_copy_overwrite_keeps_destination_present_throughout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a file destination never leaves it missing, even briefly."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    original_replace = Path.replace
    observed_missing: list[bool] = []

    def observing_replace(self: Path, target: str | Path) -> Path:
        """Record whether the destination is absent before each rename."""
        observed_missing.append(not destination.exists())
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", observing_replace)

    atomic_copy(source=source, destination=destination, backup=False)

    monkeypatch.setattr(Path, "replace", original_replace)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("new\n")
    assert_that(observed_missing).does_not_contain(True)


def test_copy_overwrite_failure_removes_partial_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed overwrite staging copy removes its partial tombstone."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    def fail_destination_staging_copy(
        *,
        source: Path,
        destination: Path,
    ) -> None:
        """Write a partial tombstone, then fail staging the overwritten file."""
        if source == tmp_path / "destination.txt":
            destination.write_text("partial\n", encoding="utf-8")
            raise OSError("staging copy failed")
        copy_path_helper(source=source, destination=destination)

    monkeypatch.setattr(transaction_module, "copy_path", fail_destination_staging_copy)

    with pytest.raises(FileSystemOperationError):
        atomic_copy(source=source, destination=destination, backup=False)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that([path.name for path in tmp_path.iterdir()]).contains_only(
        "source.txt",
        "destination.txt",
    )


def test_rollback_restores_overwritten_file_content(tmp_path: Path) -> None:
    """Rolling back an overwriting copy restores the original file content."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        with transactional_file_ops(backup=False) as transaction:
            transaction.copy(source=source, destination=destination)
            assert_that(destination.read_text(encoding="utf-8")).is_equal_to("new\n")
            raise RuntimeError("abort batch")

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that(list(tmp_path.iterdir())).is_length(2)


def test_rollback_or_raise_raises_on_failed_rollback_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rollback_or_raise surfaces rollback failures as a structured error."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    def failing_cleanup(path: Path) -> None:
        """Fail every cleanup to simulate an unrecoverable rollback."""
        raise OSError("cleanup failed")

    transaction = transactional_file_ops(backup=False)
    with transaction as active_transaction:
        active_transaction.copy(source=source, destination=destination)
        monkeypatch.setattr(transaction_module, "_cleanup_path", failing_cleanup)
        with pytest.raises(FileSystemRollbackError):
            active_transaction.rollback_or_raise()


def _tree_snapshot(root: Path) -> dict[str, str | None]:
    """Capture relative paths and file contents beneath a directory tree."""
    snapshot: dict[str, str | None] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_file() and not path.is_symlink():
            snapshot[relative] = path.read_text(encoding="utf-8")
        else:
            snapshot[relative] = None
    return snapshot


def test_copy_directory_tree_syncs_and_commits(tmp_path: Path) -> None:
    """Copying a nested directory tree stages, syncs, and commits every entry."""
    source = tmp_path / "tree"
    (source / "nested" / "deep").mkdir(parents=True)
    (source / "file.txt").write_text("top\n", encoding="utf-8")
    (source / "nested" / "leaf.txt").write_text("leaf\n", encoding="utf-8")
    (source / "nested" / "deep" / "buried.txt").write_text("buried\n", encoding="utf-8")
    destination = tmp_path / "copied"

    log = atomic_copy(source=source, destination=destination, backup=False)

    assert_that(log.operation).is_equal_to(FileOperation.COPY)
    assert_that((destination / "file.txt").read_text(encoding="utf-8")).is_equal_to(
        "top\n",
    )
    assert_that(
        (destination / "nested" / "deep" / "buried.txt").read_text(encoding="utf-8"),
    ).is_equal_to("buried\n")
    assert_that([path.name for path in tmp_path.iterdir()]).contains_only(
        "tree",
        "copied",
    )


def test_copy_rejects_directory_destination_inside_source_tree(
    tmp_path: Path,
) -> None:
    """Copying a directory beneath itself is rejected without touching the tree."""
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "file.txt").write_text("payload\n", encoding="utf-8")
    (source / "nested" / "leaf.txt").write_text("leaf\n", encoding="utf-8")
    destination = source / "clone"

    snapshot_before = _tree_snapshot(tmp_path)

    with pytest.raises(FileSystemOperationError):
        atomic_copy(source=source, destination=destination, backup=False)

    assert_that(_tree_snapshot(tmp_path)).is_equal_to(snapshot_before)


def test_move_rejects_directory_destination_inside_source_tree_on_exdev(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EXDEV move fallback rejects a destination inside the source tree."""
    source = tmp_path / "tree"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "file.txt").write_text("payload\n", encoding="utf-8")
    destination = nested / "clone"

    original_replace = Path.replace

    def cross_device_replace(self: Path, target: str | Path) -> Path:
        """Force the EXDEV copy fallback when staging the source rename."""
        if self == source and Path(target).parent == nested:
            raise OSError(errno.EXDEV, "cross-device link")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", cross_device_replace)

    snapshot_before = _tree_snapshot(tmp_path)

    with pytest.raises(FileSystemOperationError):
        atomic_move(source=source, destination=destination, backup=False)

    monkeypatch.setattr(Path, "replace", original_replace)

    assert_that(_tree_snapshot(tmp_path)).is_equal_to(snapshot_before)


def test_rollback_or_raise_passes_when_rollback_succeeds(tmp_path: Path) -> None:
    """rollback_or_raise is silent when every rollback step succeeds."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    transaction = transactional_file_ops(backup=False)
    with transaction as active_transaction:
        active_transaction.copy(source=source, destination=destination)
        active_transaction.rollback_or_raise()

    assert_that(destination.exists()).is_false()
    assert_that(source.read_text(encoding="utf-8")).is_equal_to("payload\n")
