"""Tests for the durable SQLite saga session log."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import SagaError
from winnow.fs import FileOperation, OperationLog
from winnow.pipeline import (
    CommandStatus,
    MoveFile,
    SagaLog,
    SessionStatus,
)
from winnow.pipeline import _saga_store as saga_store
from winnow.pipeline.saga_schema import SCHEMA_VERSION
from winnow.storage import read_schema_version

DIGEST = "0" * 64
FUTURE = datetime.now(UTC) + timedelta(days=1)
PAST = datetime.now(UTC) - timedelta(days=1)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return the database path used by the log fixtures.

    Args:
        tmp_path: Per-test scratch directory.

    Returns:
        Path to a not-yet-created ``s.db``.
    """
    return tmp_path / "s.db"


@pytest.fixture
def saga_log(db_path: Path) -> Iterator[SagaLog]:
    """Yield an open file-backed saga log.

    Args:
        db_path: Database path fixture.

    Yields:
        An open :class:`SagaLog`, closed on teardown.
    """
    log = SagaLog(db_path)
    try:
        yield log
    finally:
        log.close()


def _move(tmp_path: Path, name: str) -> MoveFile:
    """Build a move command between two paths under ``tmp_path``.

    Args:
        tmp_path: Directory to place the paths in.
        name: Base name of the source file.

    Returns:
        An unexecuted :class:`MoveFile`.
    """
    return MoveFile(source=tmp_path / name, destination=tmp_path / f"{name}.moved")


def _new_session(log: SagaLog, tmp_path: Path) -> str:
    """Create a session with placeholder paths.

    Args:
        log: Log to record the session in.
        tmp_path: Directory used for source and destination.

    Returns:
        The new session identifier.
    """
    return log.create_session(
        config_digest=DIGEST,
        source=tmp_path / "src",
        destination=tmp_path / "dst",
    )


def _finished_session(log: SagaLog, tmp_path: Path, status: SessionStatus) -> str:
    """Create a session and immediately finish it with ``status``.

    Args:
        log: Log to record the session in.
        tmp_path: Directory used for source and destination.
        status: Terminal status to apply.

    Returns:
        The session identifier.
    """
    session_id = _new_session(log, tmp_path)
    log.finish_session(session_id=session_id, status=status)
    return session_id


def test_open_creates_schema_tables(saga_log: SagaLog, db_path: Path) -> None:
    """Opening a fresh log creates the version, sessions and commands tables."""
    with closing(sqlite3.connect(db_path)) as raw:
        names = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = [row[1] for row in raw.execute("PRAGMA table_info(commands)")]
        version = read_schema_version(raw)

    assert_that(names).contains("schema_version", "sessions", "commands")
    assert_that(columns).is_equal_to(
        [
            "seq",
            "session_id",
            "command_type",
            "args_json",
            "log_json",
            "status",
            "started_at",
            "completed_at",
        ],
    )
    assert_that(version).is_equal_to(SCHEMA_VERSION)


def test_open_rejects_newer_schema(db_path: Path) -> None:
    """A database written by a newer build is refused with a SagaError."""
    with closing(sqlite3.connect(db_path)) as raw:
        raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        raw.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION + 1,))
        raw.commit()

    with pytest.raises(SagaError) as error:
        SagaLog(db_path)

    assert_that(error.value.context.details).contains_entry(
        {"found": SCHEMA_VERSION + 1},
    )


def test_default_path_lives_under_user_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a path the log is ``sessions.db`` in the (created) data dir."""
    monkeypatch.setenv("WINNOW_DATA_DIR", str(tmp_path / "data"))

    with SagaLog() as log:
        path = log.path

    assert_that(path).is_equal_to(tmp_path / "data" / "sessions.db")
    assert_that((tmp_path / "data" / "sessions.db").is_file()).is_true()


def test_memory_log_round_trips(tmp_path: Path) -> None:
    """An in-memory log supports the full write/read cycle."""
    with SagaLog(":memory:") as log:
        session_id = _new_session(log, tmp_path)
        seq = log.append_command(session_id=session_id, command=_move(tmp_path, "a"))

        assert_that(log.path).is_equal_to(":memory:")
        assert_that(log.list_commands(session_id)).extracting("seq").contains(seq)


def test_session_id_format(saga_log: SagaLog, tmp_path: Path) -> None:
    """Session ids are a UTC timestamp and eight hex characters."""
    session_id = _new_session(saga_log, tmp_path)

    assert_that(session_id).matches(r"^\d{8}T\d{6}-[0-9a-f]{8}$")


def test_create_session_records_running_session(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """A new session is ``running`` with its inputs and no commands."""
    session_id = _new_session(saga_log, tmp_path)

    record = saga_log.get_session(session_id)

    assert_that(record).is_not_none()
    if record is None:
        pytest.fail("expected a session record")
    assert_that(record.status).is_equal_to(SessionStatus.RUNNING)
    assert_that(record.completed_at).is_none()
    assert_that(record.command_count).is_equal_to(0)
    assert_that(record.config_digest).is_equal_to(DIGEST)
    assert_that(record.source).is_equal_to(str(tmp_path / "src"))
    assert_that(record.destination).is_equal_to(str(tmp_path / "dst"))
    assert_that(record.started_at).matches(r"\+00:00$")


def test_get_session_unknown_returns_none(saga_log: SagaLog) -> None:
    """Looking up an unknown id yields ``None`` rather than raising."""
    assert_that(saga_log.get_session("missing")).is_none()


def test_append_and_mark_done_populate_command_records(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """Commands are stored ``in_progress`` and gain their log when done."""
    session_id = _new_session(saga_log, tmp_path)
    command = _move(tmp_path, "a")
    seq = saga_log.append_command(session_id=session_id, command=command)
    pending = saga_log.list_commands(session_id)[0]

    op_log = OperationLog(
        operation=FileOperation.MOVE,
        source=command.source,
        destination=command.destination,
    )
    saga_log.mark_done(seq=seq, log=op_log)
    done = saga_log.list_commands(session_id)[0]

    assert_that(pending.status).is_equal_to(CommandStatus.IN_PROGRESS)
    assert_that(pending.log).is_none()
    assert_that(pending.completed_at).is_none()
    assert_that(pending.command_type).is_equal_to("move_file")
    assert_that(pending.args).is_equal_to({**command.to_dict(), "schema": 1})
    assert_that(done.status).is_equal_to(CommandStatus.DONE)
    assert_that(done.log).is_equal_to(op_log.as_dict())
    assert_that(done.completed_at).is_not_none()
    assert_that(done.timestamp).is_equal_to(pending.timestamp)


def test_list_commands_orders_and_pages_by_seq(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """Commands come back in ``seq`` order and ``limit``/``offset`` page them."""
    session_id = _new_session(saga_log, tmp_path)
    seqs = [
        saga_log.append_command(session_id=session_id, command=_move(tmp_path, name))
        for name in ("a", "b", "c")
    ]

    all_records = saga_log.list_commands(session_id)
    page = saga_log.list_commands(session_id, limit=1, offset=1)
    tail = saga_log.list_commands(session_id, offset=2)
    session = saga_log.get_session(session_id)

    assert_that(all_records).extracting("seq").is_equal_to(seqs)
    assert_that(page).extracting("seq").is_equal_to([seqs[1]])
    assert_that(tail).extracting("seq").is_equal_to([seqs[2]])
    assert_that(session).is_not_none()
    assert_that(getattr(session, "command_count", None)).is_equal_to(3)


def test_mark_command_and_finish_session_update_status(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """Explicit status changes stamp ``completed_at`` on both tables."""
    session_id = _new_session(saga_log, tmp_path)
    seq = saga_log.append_command(session_id=session_id, command=_move(tmp_path, "a"))

    saga_log.mark_command(seq=seq, status=CommandStatus.FAILED)
    saga_log.finish_session(session_id=session_id, status=SessionStatus.FAILED)

    command = saga_log.list_commands(session_id)[0]
    session = saga_log.get_session(session_id)
    assert_that(command.status).is_equal_to(CommandStatus.FAILED)
    assert_that(command.completed_at).is_not_none()
    assert_that(getattr(session, "status", None)).is_equal_to(SessionStatus.FAILED)
    assert_that(getattr(session, "completed_at", None)).is_not_none()


def test_list_sessions_newest_first_with_limit(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """Sessions list newest first and honour ``limit``."""
    first = _new_session(saga_log, tmp_path)
    second = _new_session(saga_log, tmp_path)
    third = _new_session(saga_log, tmp_path)

    assert_that(saga_log.list_sessions()).extracting("session_id").is_equal_to(
        [third, second, first],
    )
    assert_that(saga_log.list_sessions(limit=1)).extracting(
        "session_id",
    ).is_equal_to([third])


def test_latest_completed_session_ignores_other_statuses(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """Only ``completed`` sessions qualify, newest completion first."""
    _finished_session(saga_log, tmp_path, SessionStatus.COMPLETED)
    completed = _finished_session(saga_log, tmp_path, SessionStatus.COMPLETED)
    _finished_session(saga_log, tmp_path, SessionStatus.ROLLED_BACK)
    _finished_session(saga_log, tmp_path, SessionStatus.INTERRUPTED)
    _new_session(saga_log, tmp_path)

    latest = saga_log.latest_completed_session()

    assert_that(getattr(latest, "session_id", None)).is_equal_to(completed)


def test_latest_completed_session_none_when_empty(saga_log: SagaLog) -> None:
    """An empty log has no latest completed session."""
    assert_that(saga_log.latest_completed_session()).is_none()


def test_reopen_marks_running_work_interrupted(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """A leftover running session and in-progress command become interrupted."""
    with SagaLog(db_path) as first:
        session_id = _new_session(first, tmp_path)
        first.append_command(session_id=session_id, command=_move(tmp_path, "a"))

    with SagaLog(db_path) as reopened:
        session = reopened.get_session(session_id)
        command = reopened.list_commands(session_id)[0]
        again = reopened.mark_interrupted()

    assert_that(getattr(session, "status", None)).is_equal_to(
        SessionStatus.INTERRUPTED,
    )
    assert_that(getattr(session, "completed_at", None)).is_not_none()
    assert_that(command.status).is_equal_to(CommandStatus.INTERRUPTED)
    assert_that(again).is_equal_to(0)


def test_mark_interrupted_returns_rows_changed(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """One running session plus one in-progress command counts as two rows."""
    session_id = _new_session(saga_log, tmp_path)
    saga_log.append_command(session_id=session_id, command=_move(tmp_path, "a"))

    assert_that(saga_log.mark_interrupted()).is_equal_to(2)


def test_count_interrupted_respects_before(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """``before`` bounds the count by ``started_at``; naive datetimes are UTC."""
    _finished_session(saga_log, tmp_path, SessionStatus.INTERRUPTED)
    _finished_session(saga_log, tmp_path, SessionStatus.COMPLETED)

    assert_that(saga_log.count_interrupted()).is_equal_to(1)
    assert_that(saga_log.count_interrupted(before=FUTURE)).is_equal_to(1)
    assert_that(saga_log.count_interrupted(before=PAST)).is_equal_to(0)
    assert_that(
        saga_log.count_interrupted(before=FUTURE.replace(tzinfo=None)),
    ).is_equal_to(1)


def test_delete_sessions_keeps_interrupted_and_running_by_default(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """Finished sessions go; interrupted and running ones stay."""
    completed = _finished_session(saga_log, tmp_path, SessionStatus.COMPLETED)
    interrupted = _finished_session(saga_log, tmp_path, SessionStatus.INTERRUPTED)
    running = _new_session(saga_log, tmp_path)

    deleted = saga_log.delete_sessions(before=FUTURE)

    assert_that(deleted).is_equal_to(1)
    assert_that(saga_log.get_session(completed)).is_none()
    assert_that(saga_log.list_sessions()).extracting("session_id").contains_only(
        interrupted,
        running,
    )


def test_delete_sessions_include_interrupted_cascades_commands(
    saga_log: SagaLog,
    db_path: Path,
    tmp_path: Path,
) -> None:
    """Deleting an interrupted session removes its command rows too."""
    session_id = _new_session(saga_log, tmp_path)
    saga_log.append_command(session_id=session_id, command=_move(tmp_path, "a"))
    saga_log.finish_session(session_id=session_id, status=SessionStatus.INTERRUPTED)

    deleted = saga_log.delete_sessions(include_interrupted=True)

    with closing(sqlite3.connect(db_path)) as raw:
        remaining = raw.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    assert_that(deleted).is_equal_to(1)
    assert_that(saga_log.list_commands(session_id)).is_empty()
    assert_that(remaining).is_equal_to(0)


def test_delete_sessions_before_cutoff_keeps_newer(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """A cutoff in the past deletes nothing that started afterwards."""
    _finished_session(saga_log, tmp_path, SessionStatus.COMPLETED)

    assert_that(saga_log.delete_sessions(before=PAST)).is_equal_to(0)
    assert_that(saga_log.list_sessions()).is_length(1)


def test_write_while_locked_raises_saga_error(
    saga_log: SagaLog,
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write blocked by another writer fails fast with the lock message."""
    monkeypatch.setattr(saga_store, "CONNECT_TIMEOUT", 0.1)
    other = SagaLog(db_path)
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = datetime.now(UTC)
        with pytest.raises(SagaError) as error:
            _new_session(other, tmp_path)
        elapsed = datetime.now(UTC) - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        other.close()

    assert_that(str(error.value)).contains(
        "another winnow session holds the transaction log",
    )
    assert_that(error.value.context.operation).is_equal_to(
        "saga_log.create_session",
    )
    assert_that(elapsed).is_less_than(timedelta(seconds=5))
    assert_that(_new_session(saga_log, tmp_path)).is_not_empty()


def test_failed_write_rolls_back_and_wraps(
    saga_log: SagaLog,
    tmp_path: Path,
) -> None:
    """A constraint violation is wrapped and leaves no partial row behind."""
    with pytest.raises(SagaError) as error:
        saga_log.append_command(session_id="ghost", command=_move(tmp_path, "a"))

    assert_that(error.value.context.operation).is_equal_to(
        "saga_log.append_command",
    )
    assert_that(error.value.__cause__).is_instance_of(sqlite3.Error)
    assert_that(saga_log.list_commands("ghost")).is_empty()


def test_context_manager_closes_log(db_path: Path, tmp_path: Path) -> None:
    """Leaving the ``with`` block closes the log; later use raises."""
    with SagaLog(db_path) as log:
        session_id = _new_session(log, tmp_path)

    with pytest.raises(SagaError) as error:
        log.get_session(session_id)

    assert_that(str(error.value)).contains("closed")
    assert_that(re.search(r"saga_log\.get_session", str(error.value))).is_not_none()
