"""Tests for reversible pipeline filesystem commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import PipelineError
from winnow.fs import BackupOptions, FileOperation
from winnow.pipeline import (
    Command,
    CopyFile,
    CreateDirectory,
    DeleteFile,
    MoveFile,
)


@pytest.fixture
def backup_options(tmp_path: Path) -> BackupOptions:
    """Return backup options that store backups outside the working tree."""
    return BackupOptions(directory=tmp_path / "backups")


def test_move_file_execute_moves_source_to_destination(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """MoveFile relocates content and reports the move operation."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    command = MoveFile(
        source=source,
        destination=destination,
        backup=backup_options,
    )
    log = command.execute()

    assert_that(source.exists()).is_false()
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("payload\n")
    assert_that(log.operation).is_equal_to(FileOperation.MOVE)


def test_move_file_undo_restores_source(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """Undoing a move restores the original source location."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    command = MoveFile(
        source=source,
        destination=destination,
        backup=backup_options,
    )
    command.execute()
    command.undo()

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("payload\n")
    assert_that(destination.exists()).is_false()


def test_move_file_undo_restores_overwritten_destination(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """Undoing a move that overwrote a destination restores both files."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    command = MoveFile(
        source=source,
        destination=destination,
        backup=backup_options,
    )
    command.execute()
    command.undo()

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("new\n")
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")


def test_copy_file_execute_keeps_source_and_creates_copy(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """CopyFile duplicates content while leaving the source in place."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    command = CopyFile(
        source=source,
        destination=destination,
        backup=backup_options,
    )
    command.execute()

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("payload\n")
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("payload\n")


def test_copy_file_undo_removes_new_copy(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """Undoing a copy to a fresh destination removes only the copy."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    command = CopyFile(
        source=source,
        destination=destination,
        backup=backup_options,
    )
    command.execute()
    command.undo()

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("payload\n")
    assert_that(destination.exists()).is_false()


def test_copy_file_undo_restores_overwritten_destination(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """Undoing a copy that overwrote a destination restores prior content."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    command = CopyFile(
        source=source,
        destination=destination,
        backup=backup_options,
    )
    command.execute()
    command.undo()

    assert_that(source.read_text(encoding="utf-8")).is_equal_to("new\n")
    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")


def test_delete_file_execute_and_undo_round_trip(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """DeleteFile removes a path and undo restores it from backup."""
    target = tmp_path / "target.txt"
    target.write_text("payload\n", encoding="utf-8")

    command = DeleteFile(path=target, backup=backup_options)
    command.execute()

    assert_that(target.exists()).is_false()

    command.undo()

    assert_that(target.read_text(encoding="utf-8")).is_equal_to("payload\n")


def test_delete_file_undo_without_backup_raises(tmp_path: Path) -> None:
    """DeleteFile without a backup cannot be undone and raises PipelineError."""
    target = tmp_path / "target.txt"
    target.write_text("payload\n", encoding="utf-8")

    command = DeleteFile(path=target, backup=False)
    command.execute()

    with pytest.raises(PipelineError) as excinfo:
        command.undo()

    assert_that(str(excinfo.value)).contains("without a retained backup")


def test_create_directory_execute_and_undo(tmp_path: Path) -> None:
    """CreateDirectory builds nested directories and undo removes them."""
    target = tmp_path / "nested" / "child" / "leaf"

    command = CreateDirectory(path=target)
    command.execute()

    assert_that(target.is_dir()).is_true()

    command.undo()

    assert_that((tmp_path / "nested").exists()).is_false()


def test_create_directory_exist_ok_undo_is_noop(tmp_path: Path) -> None:
    """CreateDirectory with exist_ok preserves a pre-existing directory on undo."""
    target = tmp_path / "existing"
    target.mkdir()

    command = CreateDirectory(path=target, exist_ok=True)
    command.execute()
    command.undo()

    assert_that(target.is_dir()).is_true()


def test_create_directory_undo_rejects_non_empty_directory(tmp_path: Path) -> None:
    """Undo fails when a created directory gained content after execute."""
    target = tmp_path / "created"

    command = CreateDirectory(path=target)
    command.execute()
    (target / "file.txt").write_text("data\n", encoding="utf-8")

    with pytest.raises(PipelineError) as excinfo:
        command.undo()

    assert_that(excinfo.value.context.operation).is_equal_to(
        "pipeline.create_directory.undo",
    )
    assert_that(target.is_dir()).is_true()


def test_execute_twice_raises_pipeline_error(
    tmp_path: Path,
    backup_options: BackupOptions,
) -> None:
    """Re-executing an already executed command raises PipelineError."""
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("payload\n", encoding="utf-8")

    command = MoveFile(
        source=source,
        destination=destination,
        backup=backup_options,
    )
    command.execute()

    with pytest.raises(PipelineError) as excinfo:
        command.execute()

    assert_that(str(excinfo.value)).contains("already been executed")


def test_undo_before_execute_raises_pipeline_error(tmp_path: Path) -> None:
    """Undoing a command that never executed raises PipelineError."""
    command = DeleteFile(path=tmp_path / "target.txt")

    with pytest.raises(PipelineError) as excinfo:
        command.undo()

    assert_that(str(excinfo.value)).contains("has not been executed")


def test_execute_failure_wraps_error_and_leaves_filesystem_unchanged(
    tmp_path: Path,
) -> None:
    """A failed execute raises PipelineError and mutates nothing."""
    missing = tmp_path / "missing.txt"
    destination = tmp_path / "destination.txt"

    command = MoveFile(source=missing, destination=destination)

    with pytest.raises(PipelineError) as excinfo:
        command.execute()

    assert_that(excinfo.value.context.operation).is_equal_to(
        "pipeline.move_file.execute",
    )
    assert_that(missing.exists()).is_false()
    assert_that(destination.exists()).is_false()


@pytest.mark.parametrize(
    "command",
    [
        MoveFile(source=Path("a.txt"), destination=Path("b.txt")),
        MoveFile(
            source=Path("a.txt"),
            destination=Path("b.txt"),
            backup=BackupOptions(directory=Path("bak"), suffix=".old"),
        ),
        CopyFile(source=Path("a.txt"), destination=Path("b.txt")),
        DeleteFile(path=Path("a.txt")),
        DeleteFile(path=Path("a.txt"), backup=False),
        CreateDirectory(path=Path("dir"), parents=False, exist_ok=True),
    ],
    ids=[
        "move",
        "move_with_backup_options",
        "copy",
        "delete",
        "delete_no_backup",
        "create_directory",
    ],
)
def test_command_serialization_round_trips(command: Command) -> None:
    """Serializing then deserializing a command reproduces it exactly."""
    restored = Command.from_dict(command.to_dict())

    assert_that(restored).is_equal_to(command)


def test_to_dict_includes_command_type() -> None:
    """Serialized commands are tagged with their command type name."""
    payload = MoveFile(source=Path("a"), destination=Path("b")).to_dict()

    assert_that(payload).contains_entry({"command": "move_file"})


def test_from_dict_missing_command_type_raises() -> None:
    """Deserializing without a command type raises PipelineError."""
    with pytest.raises(PipelineError) as excinfo:
        Command.from_dict({"source": "a", "destination": "b"})

    assert_that(str(excinfo.value)).contains("missing a 'command' type")


def test_from_dict_unknown_command_type_raises() -> None:
    """Deserializing an unknown command type raises PipelineError."""
    with pytest.raises(PipelineError) as excinfo:
        Command.from_dict({"command": "teleport_file"})

    assert_that(str(excinfo.value)).contains("unknown pipeline command type")


def test_from_dict_missing_required_field_raises() -> None:
    """Deserializing a command without required paths raises PipelineError."""
    with pytest.raises(PipelineError) as excinfo:
        Command.from_dict({"command": "move_file", "source": "a"})

    assert_that(str(excinfo.value)).contains("missing string field 'destination'")
