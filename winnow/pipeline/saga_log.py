"""Durable SQLite session log for the saga coordinator.

:class:`SagaLog` is the only place the saga issues SQL. Every session and
every command executed inside it is recorded before and after it runs, so a
crashed run leaves an ``interrupted`` trail that a later process can inspect
or undo. Connection handling lives in :class:`~winnow.pipeline._saga_store.SagaStore`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from winnow.exceptions import SagaError
from winnow.fs import OperationLog
from winnow.pipeline._saga_store import MSG_WRITE_FAILED, SagaStore
from winnow.pipeline.commands import Command
from winnow.pipeline.saga_records import (
    CommandRecord,
    CommandStatus,
    SessionRecord,
    SessionStatus,
)
from winnow.pipeline.saga_rows import (
    SELECT_COMMANDS,
    SELECT_SESSIONS,
    command_from_row,
    new_session_id,
    session_from_row,
    to_iso,
    utc_now,
)
from winnow.pipeline.saga_schema import ARGS_SCHEMA_VERSION


class SagaLog:
    """Append-only session and command log backed by SQLite.

    Opening the log provisions or migrates the schema and immediately marks
    any ``running`` session and ``in_progress`` command left behind by a
    previous process as ``interrupted``.

    Args:
        path: Database file, or ``":memory:"`` for a transient log. Defaults to
            ``user_data_dir() / "sessions.db"``; parent directories are
            created on demand.

    Raises:
        SagaError: If the database cannot be opened or its schema provisioned.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._store = SagaStore(path)
        try:
            self.mark_interrupted()
        except Exception:
            self.close()
            raise

    @property
    def path(self) -> Path | str:
        """Return the database location.

        Returns:
            Database path, or ``":memory:"``.
        """
        return self._store.path

    def close(self) -> None:
        """Close the connection; later calls raise :class:`SagaError`."""
        self._store.close()

    def __enter__(self) -> Self:
        """Return the open log for use in a ``with`` block.

        Returns:
            This log.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the log on context exit.

        Args:
            exc_type: Exception type raised in the block, if any.
            exc_value: Exception raised in the block, if any.
            traceback: Traceback of the exception, if any.
        """
        self.close()

    def create_session(
        self,
        *,
        config_digest: str,
        source: Path,
        destination: Path,
    ) -> str:
        """Record a new ``running`` session.

        Args:
            config_digest: Digest of the active configuration.
            source: Directory the session reads from.
            destination: Directory the session writes to.

        Returns:
            The new session identifier.

        Raises:
            SagaError: If the row cannot be written.
        """
        session_id = new_session_id()
        with self._store.transaction("create_session") as connection:
            connection.execute(
                "INSERT INTO sessions (session_id, started_at, completed_at, "
                "status, config_digest, source, destination) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?);",
                (
                    session_id,
                    utc_now(),
                    SessionStatus.RUNNING.value,
                    config_digest,
                    str(source),
                    str(destination),
                ),
            )
        return session_id

    def append_command(self, *, session_id: str, command: Command) -> int:
        """Record a command as ``in_progress`` before it executes.

        Args:
            session_id: Session the command belongs to.
            command: Command about to run; ``to_dict()`` is stored.

        Returns:
            The command's sequence number.

        Raises:
            SagaError: If the row cannot be written.
        """
        args = {**command.to_dict(), "schema": ARGS_SCHEMA_VERSION}
        with self._store.transaction("append_command") as connection:
            cursor = connection.execute(
                "INSERT INTO commands (session_id, command_type, args_json, "
                "log_json, status, started_at, completed_at) "
                "VALUES (?, ?, ?, NULL, ?, ?, NULL);",
                (
                    session_id,
                    command.command_name,
                    json.dumps(args, sort_keys=True),
                    CommandStatus.IN_PROGRESS.value,
                    utc_now(),
                ),
            )
            row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover - sqlite always sets it on INSERT
            raise SagaError(MSG_WRITE_FAILED, operation="saga_log.append_command")
        return int(row_id)

    def mark_done(self, *, seq: int, log: OperationLog) -> None:
        """Store a command's operation log and mark it ``done``.

        Args:
            seq: Sequence number returned by :meth:`append_command`.
            log: Operation log produced by ``Command.execute()``.

        Raises:
            SagaError: If the row cannot be written.
        """
        with self._store.transaction("mark_done") as connection:
            connection.execute(
                "UPDATE commands SET log_json = ?, status = ?, completed_at = ? "
                "WHERE seq = ?;",
                (
                    json.dumps(log.as_dict(), sort_keys=True),
                    CommandStatus.DONE.value,
                    utc_now(),
                    seq,
                ),
            )

    def mark_command(self, *, seq: int, status: CommandStatus) -> None:
        """Set a command's status and stamp ``completed_at``.

        Args:
            seq: Sequence number of the command.
            status: New lifecycle state.

        Raises:
            SagaError: If the row cannot be written.
        """
        with self._store.transaction("mark_command") as connection:
            connection.execute(
                "UPDATE commands SET status = ?, completed_at = ? WHERE seq = ?;",
                (status.value, utc_now(), seq),
            )

    def finish_session(self, *, session_id: str, status: SessionStatus) -> None:
        """Set a session's final status and stamp ``completed_at``.

        Args:
            session_id: Session to finish.
            status: Terminal lifecycle state.

        Raises:
            SagaError: If the row cannot be written.
        """
        with self._store.transaction("finish_session") as connection:
            connection.execute(
                "UPDATE sessions SET status = ?, completed_at = ? "
                "WHERE session_id = ?;",
                (status.value, utc_now(), session_id),
            )

    def mark_interrupted(self) -> int:
        """Flag leftover ``running``/``in_progress`` rows as ``interrupted``.

        Returns:
            Number of session and command rows changed.

        Raises:
            SagaError: If the rows cannot be written.
        """
        now = utc_now()
        with self._store.transaction("mark_interrupted") as connection:
            commands = connection.execute(
                "UPDATE commands SET status = ?, completed_at = ? WHERE status = ?;",
                (CommandStatus.INTERRUPTED.value, now, CommandStatus.IN_PROGRESS.value),
            ).rowcount
            sessions = connection.execute(
                "UPDATE sessions SET status = ?, completed_at = ? WHERE status = ?;",
                (SessionStatus.INTERRUPTED.value, now, SessionStatus.RUNNING.value),
            ).rowcount
        return int(commands) + int(sessions)

    def delete_sessions(
        self,
        *,
        before: datetime | None = None,
        include_interrupted: bool = False,
    ) -> int:
        """Delete finished sessions and, by cascade, their commands.

        ``running`` sessions are never deleted. ``interrupted`` sessions are
        kept unless ``include_interrupted`` is set, since they may still be
        undone.

        Args:
            before: Only delete sessions started before this instant.
            include_interrupted: Also delete ``interrupted`` sessions.

        Returns:
            Number of sessions deleted.

        Raises:
            SagaError: If the rows cannot be deleted.
        """
        kept = (
            SessionStatus.RUNNING if include_interrupted else SessionStatus.INTERRUPTED
        )
        cutoff = None if before is None else to_iso(before)
        with self._store.transaction("delete_sessions") as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE status NOT IN (?, ?) "
                "AND (? IS NULL OR started_at < ?);",
                (SessionStatus.RUNNING.value, kept.value, cutoff, cutoff),
            )
            return int(cursor.rowcount)

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Look up one session.

        Args:
            session_id: Identifier returned by :meth:`create_session`.

        Returns:
            The session, or ``None`` when unknown.
        """
        rows = self._store.query(
            "get_session",
            f"{SELECT_SESSIONS} WHERE s.session_id = ?;",
            (session_id,),
        )
        return session_from_row(rows[0]) if rows else None

    def list_sessions(self, *, limit: int | None = None) -> list[SessionRecord]:
        """List sessions, newest first.

        Args:
            limit: Maximum number of sessions to return; ``None`` for all.

        Returns:
            Session records ordered by ``started_at`` descending.
        """
        rows = self._store.query(
            "list_sessions",
            f"{SELECT_SESSIONS} ORDER BY s.started_at DESC, s.rowid DESC LIMIT ?;",
            (-1 if limit is None else limit,),
        )
        return [session_from_row(row) for row in rows]

    def list_commands(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CommandRecord]:
        """List a session's commands in execution order.

        Args:
            session_id: Session to inspect.
            limit: Maximum number of commands to return; ``None`` for all.
            offset: Number of leading commands to skip.

        Returns:
            Command records ordered by ``seq`` ascending.
        """
        rows = self._store.query(
            "list_commands",
            f"{SELECT_COMMANDS} WHERE session_id = ? ORDER BY seq ASC "
            "LIMIT ? OFFSET ?;",
            (session_id, -1 if limit is None else limit, offset),
        )
        return [command_from_row(row) for row in rows]

    def latest_completed_session(self) -> SessionRecord | None:
        """Return the most recently finished ``completed`` session.

        Returns:
            The newest session with status ``completed``, or ``None``.
        """
        rows = self._store.query(
            "latest_completed_session",
            f"{SELECT_SESSIONS} WHERE s.status = ? "
            "ORDER BY s.completed_at DESC, s.rowid DESC LIMIT 1;",
            (SessionStatus.COMPLETED.value,),
        )
        return session_from_row(rows[0]) if rows else None

    def count_interrupted(self, *, before: datetime | None = None) -> int:
        """Count ``interrupted`` sessions.

        Args:
            before: Only count sessions started before this instant.

        Returns:
            Number of matching sessions.
        """
        cutoff = None if before is None else to_iso(before)
        rows = self._store.query(
            "count_interrupted",
            "SELECT COUNT(*) AS n FROM sessions WHERE status = ? "
            "AND (? IS NULL OR started_at < ?);",
            (SessionStatus.INTERRUPTED.value, cutoff, cutoff),
        )
        return int(rows[0]["n"])


__all__ = ["SagaLog"]
