"""Saga coordinator: begin durable sessions and undo recorded ones.

:class:`Saga` opens sessions in a :class:`~winnow.pipeline.saga_log.SagaLog`;
each :class:`~winnow.pipeline.saga_session.SagaSession` records a command as
``in_progress`` before it runs and ``done`` afterwards, so a crash between the
two is visible as ``interrupted`` in a later process. Reversal is always
``Command.undo()``, in process on failure and cross process via
:meth:`Saga.undo_session`; this module never touches the filesystem itself and
never issues SQL.
"""

from __future__ import annotations

import logging
from pathlib import Path

from winnow.exceptions import PipelineError, SagaError
from winnow.fs import OperationLog
from winnow.pipeline.commands import Command
from winnow.pipeline.saga_log import SagaLog
from winnow.pipeline.saga_records import (
    CommandRecord,
    CommandStatus,
    SessionStatus,
    UndoReport,
)
from winnow.pipeline.saga_session import SagaSession

_LOGGER = logging.getLogger(__name__)


class Saga:
    """Coordinator that begins sessions and undoes recorded ones.

    Args:
        log: Durable session log shared by every session this saga opens.
    """

    def __init__(self, log: SagaLog) -> None:
        self._log = log

    @property
    def log(self) -> SagaLog:
        """Return the underlying session log.

        Returns:
            The log passed to the constructor.
        """
        return self._log

    def begin(
        self,
        *,
        config_digest: str,
        source: Path,
        destination: Path,
    ) -> SagaSession:
        """Open a new ``running`` session.

        Args:
            config_digest: Digest of the active configuration.
            source: Directory the session reads from.
            destination: Directory the session writes to.

        Returns:
            A session ready to execute commands.

        Raises:
            SagaError: If the session row cannot be written.
        """
        session_id = self._log.create_session(
            config_digest=config_digest,
            source=source,
            destination=destination,
        )
        return SagaSession(log=self._log, session_id=session_id)

    def undo_session(self, session_id: str, *, dry_run: bool = False) -> UndoReport:
        """Reverse a recorded session's ``done`` commands, newest first.

        Each planned command is rebuilt with ``Command.from_dict`` and its
        stored :class:`OperationLog`, marked ``in_progress``, reversed with
        ``undo()`` and then marked ``undone``. Commands in any other status
        (``interrupted``, ``failed``, ``in_progress``) are never touched and
        appear in ``skipped`` with their status as the reason.

        Args:
            session_id: Session to undo.
            dry_run: Return the plan without touching the filesystem or log.

        Returns:
            The plan and, unless ``dry_run``, how many commands were reverted
            and which were skipped. A command that was reverted but whose
            ``undone`` row could not be written appears in both counts.

        Raises:
            SagaError: When the session is unknown or still ``running``.
        """
        session = self._log.get_session(session_id)
        if session is None:
            raise SagaError(
                "unknown saga session",
                operation="saga.undo_session",
                details={"session_id": session_id},
            )
        if session.status is SessionStatus.RUNNING:
            raise SagaError(
                "cannot undo a session that is still running",
                operation="saga.undo_session",
                details={"session_id": session_id},
            )
        records = self._log.list_commands(session_id)[::-1]
        planned = [record for record in records if record.status is CommandStatus.DONE]
        if dry_run:
            return UndoReport(
                session_id=session_id,
                planned=planned,
                reverted=0,
                skipped=[],
            )
        reverted = 0
        skipped: list[tuple[CommandRecord, str]] = []
        for record in records:
            if record.status is CommandStatus.DONE:
                reverted += self._undo_record(record, skipped=skipped)
            elif record.status is not CommandStatus.UNDONE:
                skipped.append((record, record.status.value))
        self._log.finish_session(
            session_id=session_id,
            status=SessionStatus.FAILED if skipped else SessionStatus.ROLLED_BACK,
        )
        return UndoReport(
            session_id=session_id,
            planned=planned,
            reverted=reverted,
            skipped=skipped,
        )

    def _undo_record(
        self,
        record: CommandRecord,
        *,
        skipped: list[tuple[CommandRecord, str]],
    ) -> int:
        """Rebuild and undo one ``done`` command.

        The row is marked ``in_progress`` before ``undo()`` touches the
        filesystem, so a stale ``done`` row can never describe an already
        reverted command: a crash or a failed ``undone`` write leaves the row
        ``in_progress``, which a later log open turns into ``interrupted`` and
        a later undo skips. A row that cannot be rebuilt, claimed or reversed
        is skipped with the reason; when only the ``undone`` write fails, the
        command counts as reverted and the log error is recorded in
        ``skipped`` so the session finishes ``failed``.

        Args:
            record: Command row to reverse.
            skipped: Collector appended to when the undo or a log write fails.

        Returns:
            ``1`` when the command was reverted, ``0`` when it was skipped.
        """
        try:
            command = _rebuild(record)
            self._log.mark_command(seq=record.seq, status=CommandStatus.IN_PROGRESS)
        except SagaError as error:
            skipped.append((record, str(error)))
            return 0
        try:
            command.undo()
        except PipelineError as error:
            skipped.append((record, str(error)))
            self._mark_quietly(seq=record.seq, status=CommandStatus.DONE)
            return 0
        try:
            self._log.mark_command(seq=record.seq, status=CommandStatus.UNDONE)
        except SagaError as error:
            skipped.append((record, f"reverted but not recorded: {error}"))
        return 1

    def _mark_quietly(self, *, seq: int, status: CommandStatus) -> None:
        """Best-effort status write that logs instead of raising.

        Args:
            seq: Command row to update.
            status: Status to store.
        """
        try:
            self._log.mark_command(seq=seq, status=status)
        except SagaError as error:
            _LOGGER.warning("could not mark command %d %s: %s", seq, status, error)


def _rebuild(record: CommandRecord) -> Command:
    """Reconstruct an executed command from its stored row.

    Args:
        record: Row with ``args`` and a populated ``log``.

    Returns:
        A command whose ``undo()`` reverses the recorded execution.

    Raises:
        SagaError: When the row has no log or the log cannot be decoded.
    """
    if record.log is None:
        raise SagaError(
            "recorded command has no operation log",
            operation="saga.undo_session",
            details={"seq": record.seq},
        )
    command = Command.from_dict(record.args)
    try:
        op_log = OperationLog.from_dict(record.log)
    except ValueError as error:
        raise SagaError(
            "recorded operation log is malformed",
            operation="saga.undo_session",
            details={"seq": record.seq, "error": str(error)},
        ) from error
    command.restore_log(op_log)
    return command


__all__ = ["Saga", "SagaSession"]
