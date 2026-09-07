"""Tests for the saga coordinator and its sessions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import PipelineError, SagaError
from winnow.pipeline import (
    CommandStatus,
    MoveFile,
    Saga,
    SagaLog,
    SagaSession,
    SessionStatus,
)

DIGEST = "0" * 64


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return the database path shared by every log in a test.

    Args:
        tmp_path: Per-test scratch directory.

    Returns:
        Path to ``sessions.db`` under the scratch directory.
    """
    return tmp_path / "sessions.db"


@pytest.fixture
def saga(db_path: Path) -> Iterator[Saga]:
    """Yield a saga over a fresh file-backed log.

    Args:
        db_path: Database path fixture.

    Yields:
        A :class:`Saga`; its log is closed on teardown.
    """
    with SagaLog(db_path) as log:
        yield Saga(log)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a source directory with two files and an empty destination.

    Args:
        tmp_path: Per-test scratch directory.

    Returns:
        ``(source, destination)`` directories.
    """
    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    destination.mkdir()
    (source / "a.txt").write_text("a\n", encoding="utf-8")
    (source / "b.txt").write_text("b\n", encoding="utf-8")
    return source, destination


def _move(source: Path, destination: Path, name: str) -> MoveFile:
    """Build a backup-free move of ``name`` from ``source`` to ``destination``.

    Args:
        source: Directory holding the file.
        destination: Directory to move it into.
        name: File name.

    Returns:
        The move command.
    """
    return MoveFile(source=source / name, destination=destination / name, backup=False)


def _begin(saga: Saga, workspace: tuple[Path, Path]) -> SagaSession:
    """Open a session over the workspace directories.

    Args:
        saga: Coordinator under test.
        workspace: ``(source, destination)`` directories.

    Returns:
        A new running session.
    """
    source, destination = workspace
    return saga.begin(config_digest=DIGEST, source=source, destination=destination)


def _session_status(log: SagaLog, session_id: str) -> SessionStatus | None:
    """Return a session's recorded status.

    Args:
        log: Log to read.
        session_id: Session to inspect.

    Returns:
        The status, or ``None`` when the session is unknown.
    """
    record = log.get_session(session_id)
    return None if record is None else record.status


def _statuses(saga: Saga, session_id: str) -> list[CommandStatus]:
    """Return the recorded command statuses of a session in seq order.

    Args:
        saga: Coordinator whose log is read.
        session_id: Session to inspect.

    Returns:
        Statuses ordered by ``seq``.
    """
    return [record.status for record in saga.log.list_commands(session_id)]


def _run_two_moves(saga: Saga, workspace: tuple[Path, Path]) -> str:
    """Commit a session that moves both workspace files.

    Args:
        saga: Coordinator under test.
        workspace: ``(source, destination)`` directories.

    Returns:
        The committed session id.
    """
    source, destination = workspace
    with _begin(saga, workspace) as session:
        session.execute(_move(source, destination, "a.txt"))
        session.execute(_move(source, destination, "b.txt"))
    return session.session_id


def test_context_manager_commits_on_clean_exit(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """Leaving the block normally moves the file and records completed/done."""
    source, destination = workspace
    with _begin(saga, workspace) as session:
        log = session.execute(_move(source, destination, "a.txt"))
        assert_that(session.executed).is_length(1)

    assert_that(log.destination).is_equal_to(destination / "a.txt")
    assert_that((destination / "a.txt").exists()).is_true()
    assert_that((source / "a.txt").exists()).is_false()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.COMPLETED
    )
    assert_that(_statuses(saga, session.session_id)).is_equal_to([CommandStatus.DONE])


def test_context_manager_rolls_back_on_exception(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """An exception after two moves restores both files and propagates."""
    source, destination = workspace
    with pytest.raises(RuntimeError, match="boom"), _begin(saga, workspace) as session:
        session.execute(_move(source, destination, "a.txt"))
        session.execute(_move(source, destination, "b.txt"))
        raise RuntimeError("boom")

    assert_that((source / "a.txt").exists()).is_true()
    assert_that((source / "b.txt").exists()).is_true()
    assert_that(list(destination.iterdir())).is_empty()
    assert_that(session.executed).is_empty()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.ROLLED_BACK
    )
    assert_that(_statuses(saga, session.session_id)).is_equal_to(
        [CommandStatus.UNDONE, CommandStatus.UNDONE],
    )


def test_failed_execute_marks_failed_and_rolls_back_earlier_moves(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """A third move with a missing source fails, is recorded, and unwinds."""
    source, destination = workspace
    with pytest.raises(PipelineError), _begin(saga, workspace) as session:
        session.execute(_move(source, destination, "a.txt"))
        session.execute(_move(source, destination, "b.txt"))
        session.execute(_move(source, destination, "missing.txt"))

    assert_that((source / "a.txt").exists()).is_true()
    assert_that((source / "b.txt").exists()).is_true()
    assert_that(_statuses(saga, session.session_id)).is_equal_to(
        [CommandStatus.UNDONE, CommandStatus.UNDONE, CommandStatus.FAILED],
    )


def test_rollback_is_idempotent(saga: Saga, workspace: tuple[Path, Path]) -> None:
    """A second rollback returns the stored errors without touching files."""
    source, destination = workspace
    session = _begin(saga, workspace)
    session.execute(_move(source, destination, "a.txt"))

    first = session.rollback()
    (source / "a.txt").unlink()
    second = session.rollback()

    assert_that(first).is_empty()
    assert_that(second).is_same_as(first)
    assert_that((source / "a.txt").exists()).is_false()


def test_rollback_after_commit_raises(saga: Saga, workspace: tuple[Path, Path]) -> None:
    """A committed session cannot be rolled back afterwards."""
    source, destination = workspace
    session = _begin(saga, workspace)
    session.execute(_move(source, destination, "a.txt"))
    session.commit()
    with pytest.raises(SagaError, match="already finished"):
        session.rollback()
    assert_that((destination / "a.txt").exists()).is_true()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.COMPLETED
    )


def test_failed_mark_done_still_rolls_back_command(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command whose ``done`` write fails is still reverted on exit."""
    source, destination = workspace

    def _boom(**_: object) -> None:
        raise SagaError("locked", operation="saga.log.mark_done")

    monkeypatch.setattr(saga.log, "mark_done", _boom)
    with pytest.raises(SagaError, match="locked"), _begin(saga, workspace) as session:
        session.execute(_move(source, destination, "a.txt"))

    assert_that((source / "a.txt").exists()).is_true()
    assert_that((destination / "a.txt").exists()).is_false()
    assert_that(session.executed).is_empty()


def test_failed_command_keeps_pipeline_error_when_failed_write_fails(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ``failed``-row write is noted on the command's PipelineError."""
    source, destination = workspace

    def _boom(**_: object) -> None:
        raise SagaError("locked", operation="saga.log.mark_command")

    monkeypatch.setattr(saga.log, "mark_command", _boom)
    with (
        pytest.raises(PipelineError) as excinfo,
        _begin(saga, workspace) as session,
    ):
        session.execute(_move(source, destination, "missing.txt"))

    assert_that(excinfo.value.__notes__).is_length(1)
    assert_that(excinfo.value.__notes__[0]).contains("locked")
    assert_that(session.executed).is_empty()
    assert_that(_statuses(saga, session.session_id)).is_equal_to(
        [CommandStatus.IN_PROGRESS],
    )
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.ROLLED_BACK
    )


def test_rollback_retries_when_finish_session_fails(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed session update is retried by the next ``rollback()`` call."""
    source, destination = workspace
    session = _begin(saga, workspace)
    session.execute(_move(source, destination, "a.txt"))
    original = saga.log.finish_session

    def _boom(**_: object) -> None:
        raise SagaError("locked", operation="saga.log.finish_session")

    monkeypatch.setattr(saga.log, "finish_session", _boom)
    with pytest.raises(SagaError, match="locked"):
        session.rollback()
    monkeypatch.setattr(saga.log, "finish_session", original)

    assert_that(session.rollback()).is_empty()
    assert_that((source / "a.txt").exists()).is_true()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.ROLLED_BACK
    )


def test_execute_after_finish_raises(saga: Saga, workspace: tuple[Path, Path]) -> None:
    """A committed session refuses further commands and a second commit."""
    source, destination = workspace
    session = _begin(saga, workspace)
    session.commit()
    with pytest.raises(SagaError, match="already finished"):
        session.execute(_move(source, destination, "a.txt"))
    with pytest.raises(SagaError, match="already finished"):
        session.commit()


def test_exit_after_manual_rollback_does_not_commit(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """Rolling back inside the block leaves the session rolled back on exit."""
    with _begin(saga, workspace) as session:
        session.rollback()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.ROLLED_BACK
    )


def test_context_manager_notes_failed_undo(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undo failures during rollback are attached to the escaping exception."""
    source, destination = workspace

    def _fail(self: MoveFile) -> None:
        raise PipelineError("undo exploded", operation="pipeline.move_file.undo")

    monkeypatch.setattr(MoveFile, "undo", _fail)
    with pytest.raises(RuntimeError) as excinfo, _begin(saga, workspace) as session:
        session.execute(_move(source, destination, "a.txt"))
        raise RuntimeError("boom")

    assert_that(excinfo.value.__notes__).is_length(1)
    assert_that(excinfo.value.__notes__[0]).starts_with(
        "rollback failed: undo exploded"
    )
    assert_that(session.executed).is_length(1)
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.FAILED
    )
    assert_that(_statuses(saga, session.session_id)).is_equal_to([CommandStatus.DONE])


def test_context_manager_keeps_original_error_when_mark_command_fails(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ``undone`` write after a successful undo is noted, not raised."""
    source, destination = workspace

    def _boom(**_: object) -> None:
        raise SagaError("locked", operation="saga.log.mark_command")

    monkeypatch.setattr(saga.log, "mark_command", _boom)
    with pytest.raises(RuntimeError) as excinfo, _begin(saga, workspace) as session:
        session.execute(_move(source, destination, "a.txt"))
        session.execute(_move(source, destination, "b.txt"))
        raise RuntimeError("boom")

    assert_that(excinfo.value.__notes__).is_length(2)
    assert_that(excinfo.value.__notes__[0]).contains("locked")
    assert_that((source / "a.txt").exists()).is_true()
    assert_that((source / "b.txt").exists()).is_true()
    assert_that(session.executed).is_empty()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.FAILED
    )


def test_context_manager_keeps_original_error_when_finish_session_fails(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed session update on exit is noted and the block error propagates."""
    source, destination = workspace

    def _boom(**_: object) -> None:
        raise SagaError("locked", operation="saga.log.finish_session")

    monkeypatch.setattr(saga.log, "finish_session", _boom)
    with pytest.raises(RuntimeError) as excinfo, _begin(saga, workspace) as session:
        session.execute(_move(source, destination, "a.txt"))
        raise RuntimeError("boom")

    assert_that(excinfo.value.__notes__).is_length(1)
    assert_that(excinfo.value.__notes__[0]).contains("locked")
    assert_that((source / "a.txt").exists()).is_true()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.RUNNING
    )


def test_undo_session_dry_run_plans_without_moving(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """A dry run lists the done commands newest first and touches nothing."""
    source, destination = workspace
    session_id = _run_two_moves(saga, workspace)

    report = saga.undo_session(session_id, dry_run=True)

    assert_that(report.session_id).is_equal_to(session_id)
    assert_that(report.reverted).is_equal_to(0)
    assert_that(report.skipped).is_empty()
    assert_that([record.args["source"] for record in report.planned]).is_equal_to(
        [str(source / "b.txt"), str(source / "a.txt")],
    )
    assert_that(sorted(path.name for path in destination.iterdir())).is_equal_to(
        ["a.txt", "b.txt"],
    )
    assert_that(_session_status(saga.log, session_id)).is_equal_to(
        SessionStatus.COMPLETED
    )


def test_undo_session_from_fresh_saga_restores_files(
    saga: Saga,
    workspace: tuple[Path, Path],
    db_path: Path,
) -> None:
    """A new Saga over the same database reverts a committed session."""
    source, destination = workspace
    session_id = _run_two_moves(saga, workspace)
    saga.log.close()

    with SagaLog(db_path) as log:
        fresh = Saga(log)
        report = fresh.undo_session(session_id)
        status = _session_status(log, session_id)
        statuses = _statuses(fresh, session_id)

    assert_that(report.reverted).is_equal_to(2)
    assert_that(report.skipped).is_empty()
    assert_that(report.planned).is_length(2)
    assert_that((source / "a.txt").exists()).is_true()
    assert_that((source / "b.txt").exists()).is_true()
    assert_that(list(destination.iterdir())).is_empty()
    assert_that(status).is_equal_to(SessionStatus.ROLLED_BACK)
    assert_that(statuses).is_equal_to([CommandStatus.UNDONE, CommandStatus.UNDONE])


def test_undo_session_skips_interrupted_command(
    saga: Saga,
    workspace: tuple[Path, Path],
    db_path: Path,
) -> None:
    """An interrupted command is never touched and is reported as skipped."""
    source, destination = workspace
    session = _begin(saga, workspace)
    session.execute(_move(source, destination, "a.txt"))
    seq = saga.log.append_command(
        session_id=session.session_id,
        command=_move(source, destination, "b.txt"),
    )
    saga.log.close()

    with SagaLog(db_path) as log:
        report = Saga(log).undo_session(session.session_id)
        status = _session_status(log, session.session_id)

    assert_that(report.reverted).is_equal_to(1)
    assert_that([(item.seq, reason) for item, reason in report.skipped]).is_equal_to(
        [(seq, "interrupted")],
    )
    assert_that((source / "a.txt").exists()).is_true()
    assert_that((source / "b.txt").exists()).is_true()
    assert_that(status).is_equal_to(SessionStatus.FAILED)


def test_undo_session_collects_undo_failures(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """A move whose destination vanished is skipped with the error message."""
    source, destination = workspace
    session_id = _run_two_moves(saga, workspace)
    (destination / "b.txt").unlink()

    report = saga.undo_session(session_id)

    assert_that(report.reverted).is_equal_to(1)
    assert_that(report.skipped).is_length(1)
    skipped_record, reason = report.skipped[0]
    assert_that(skipped_record.args["source"]).is_equal_to(str(source / "b.txt"))
    assert_that(reason).contains("move_file")
    assert_that((source / "a.txt").exists()).is_true()
    assert_that(_statuses(saga, session_id)).is_equal_to(
        [CommandStatus.UNDONE, CommandStatus.DONE],
    )


@pytest.mark.parametrize(
    ("log_json", "match"),
    [(None, "has no operation log"), ('{"status": "applied"}', "malformed")],
    ids=["missing_log", "malformed_log"],
)
def test_undo_session_skips_unusable_log_and_reverts_the_rest(
    saga: Saga,
    workspace: tuple[Path, Path],
    log_json: str | None,
    match: str,
) -> None:
    """A ``done`` row whose stored log cannot be rebuilt is skipped, not fatal."""
    source, destination = workspace
    session_id = _run_two_moves(saga, workspace)
    seq_b = saga.log.list_commands(session_id)[1].seq
    with saga.log._store.transaction("corrupt") as connection:
        connection.execute(
            "UPDATE commands SET log_json = ? WHERE seq = ?;",
            (log_json, seq_b),
        )

    report = saga.undo_session(session_id)

    assert_that(report.reverted).is_equal_to(1)
    assert_that(report.skipped).is_length(1)
    skipped_record, reason = report.skipped[0]
    assert_that(skipped_record.seq).is_equal_to(seq_b)
    assert_that(reason).contains(match)
    assert_that((source / "a.txt").exists()).is_true()
    assert_that((destination / "b.txt").exists()).is_true()
    assert_that(_session_status(saga.log, session_id)).is_equal_to(SessionStatus.FAILED)


def _fail_undone_writes(saga: Saga, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only ``mark_command(status=UNDONE)`` raise on the saga's log.

    Args:
        saga: Coordinator whose log is patched.
        monkeypatch: Pytest patcher.
    """
    original = saga.log.mark_command

    def _boom(*, seq: int, status: CommandStatus) -> None:
        if status is CommandStatus.UNDONE:
            raise SagaError("locked", operation="saga.log.mark_command")
        original(seq=seq, status=status)

    monkeypatch.setattr(saga.log, "mark_command", _boom)


def test_undo_session_records_failed_undone_write(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ``undone`` write does not stop the remaining reverts."""
    source, destination = workspace
    session_id = _run_two_moves(saga, workspace)
    _fail_undone_writes(saga, monkeypatch)

    report = saga.undo_session(session_id)

    assert_that(report.reverted).is_equal_to(2)
    assert_that(report.skipped).is_length(2)
    assert_that(report.skipped[0][1]).contains("reverted but not recorded")
    assert_that((source / "a.txt").exists()).is_true()
    assert_that((source / "b.txt").exists()).is_true()
    assert_that(_statuses(saga, session_id)).is_equal_to(
        [CommandStatus.IN_PROGRESS, CommandStatus.IN_PROGRESS],
    )
    assert_that(_session_status(saga.log, session_id)).is_equal_to(SessionStatus.FAILED)


def test_undo_session_skips_rows_left_in_progress(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """A row still ``in_progress`` is reported as skipped, never reverted."""
    source, destination = workspace
    session_id = _run_two_moves(saga, workspace)
    seq_b = saga.log.list_commands(session_id)[1].seq
    saga.log.mark_command(seq=seq_b, status=CommandStatus.IN_PROGRESS)

    report = saga.undo_session(session_id)

    assert_that(report.reverted).is_equal_to(1)
    assert_that(report.skipped).is_length(1)
    assert_that(report.skipped[0][1]).is_equal_to("in_progress")
    assert_that((destination / "b.txt").exists()).is_true()
    assert_that(_session_status(saga.log, session_id)).is_equal_to(SessionStatus.FAILED)


def test_second_undo_never_reverts_an_already_reverted_move(
    db_path: Path,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reverted-but-unrecorded overwrite move is not reverted again."""
    source, destination = workspace
    (destination / "a.txt").write_text("old\n", encoding="utf-8")
    with SagaLog(db_path) as log:
        saga = Saga(log)
        with _begin(saga, workspace) as session:
            session.execute(
                MoveFile(source=source / "a.txt", destination=destination / "a.txt"),
            )
        session_id = session.session_id
        _fail_undone_writes(saga, monkeypatch)
        first = saga.undo_session(session_id)
    monkeypatch.undo()

    with SagaLog(db_path) as log:
        second = Saga(log).undo_session(session_id)

    assert_that(first.reverted).is_equal_to(1)
    assert_that(second.reverted).is_equal_to(0)
    assert_that(second.skipped[0][1]).is_equal_to("interrupted")
    assert_that((source / "a.txt").read_text(encoding="utf-8")).is_equal_to("a\n")
    assert_that((destination / "a.txt").read_text(encoding="utf-8")).is_equal_to(
        "old\n"
    )


def test_rollback_retry_keeps_earlier_log_errors(
    saga: Saga,
    workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a failed session update still finishes ``failed``."""
    source, destination = workspace
    session = _begin(saga, workspace)
    session.execute(_move(source, destination, "a.txt"))
    _fail_undone_writes(saga, monkeypatch)
    original = saga.log.finish_session

    def _boom(**_: object) -> None:
        raise SagaError("locked", operation="saga.log.finish_session")

    monkeypatch.setattr(saga.log, "finish_session", _boom)
    with pytest.raises(SagaError, match="locked"):
        session.rollback()
    monkeypatch.setattr(saga.log, "finish_session", original)

    assert_that(session.rollback()).is_length(1)
    assert_that(session.executed).is_empty()
    assert_that((source / "a.txt").exists()).is_true()
    assert_that(_session_status(saga.log, session.session_id)).is_equal_to(
        SessionStatus.FAILED
    )


def test_undo_session_unknown_id_raises(saga: Saga) -> None:
    """An unknown session id is rejected."""
    with pytest.raises(SagaError, match="unknown saga session"):
        saga.undo_session("nope")


def test_undo_session_running_raises(
    saga: Saga,
    workspace: tuple[Path, Path],
) -> None:
    """A session still running in this process cannot be undone."""
    session = _begin(saga, workspace)
    with pytest.raises(SagaError, match="still running"):
        saga.undo_session(session.session_id)


def test_saga_log_property_returns_log(saga: Saga) -> None:
    """``Saga.log`` exposes the log passed to the constructor."""
    assert_that(saga.log).is_instance_of(SagaLog)
