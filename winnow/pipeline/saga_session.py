"""One saga session: execute commands under a durable log and roll back LIFO.

:class:`SagaSession` is created by :meth:`winnow.pipeline.saga.Saga.begin`.
It records each command as ``in_progress`` before running it and ``done``
afterwards, and reverses executed commands with ``Command.undo()`` when the
block fails. It never touches the filesystem itself and never issues SQL.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Literal, Self

from winnow.exceptions import PipelineError, SagaError
from winnow.fs import OperationLog
from winnow.pipeline.commands import Command
from winnow.pipeline.saga_log import SagaLog
from winnow.pipeline.saga_records import CommandStatus, SessionStatus

_LOGGER = logging.getLogger(__name__)


class SagaSession:
    """One recorded run of commands that commits as a unit or rolls back LIFO.

    Obtain instances through :meth:`Saga.begin`. Use as a context manager:
    leaving the block normally commits, leaving it with an exception rolls
    back every executed command and re-raises the original exception.

    Args:
        log: Session log the session records into.
        session_id: Identifier returned by ``SagaLog.create_session``.
    """

    def __init__(self, log: SagaLog, session_id: str) -> None:
        self._log = log
        self.session_id = session_id
        self._executed: list[tuple[int, Command]] = []
        self._rollback_errors: tuple[Exception, ...] | None = None
        self._finished = False

    @property
    def executed(self) -> tuple[Command, ...]:
        """Return the commands currently applied, in execution order.

        Returns:
            Commands whose effects are on disk; after a rollback only those
            whose ``undo()`` failed remain.
        """
        return tuple(command for _, command in self._executed)

    def execute(self, command: Command) -> OperationLog:
        """Record and run one command.

        The ``in_progress`` row is written before ``command.execute()`` so a
        crash in between leaves an ``interrupted`` trail.

        Args:
            command: Unexecuted command to apply.

        Returns:
            The operation log produced by the command.

        Raises:
            SagaError: When the session has already been committed or rolled
                back, or the log cannot be written.
            PipelineError: Re-raised unchanged when the command fails; the
                command is recorded as ``failed`` first.
        """
        self._require_open("execute")
        seq = self._log.append_command(session_id=self.session_id, command=command)
        try:
            op_log = command.execute()
        except PipelineError:
            self._log.mark_command(seq=seq, status=CommandStatus.FAILED)
            raise
        self._log.mark_done(seq=seq, log=op_log)
        self._executed.append((seq, command))
        return op_log

    def commit(self) -> None:
        """Mark the session ``completed``.

        Raises:
            SagaError: When the session has already finished or the log
                cannot be written.
        """
        self._require_open("commit")
        self._log.finish_session(
            session_id=self.session_id,
            status=SessionStatus.COMPLETED,
        )
        self._finished = True

    def rollback(self) -> tuple[Exception, ...]:
        """Undo every executed command, newest first, and finish the session.

        Each ``undo()`` failure is collected and the remaining commands are
        still attempted. Calling this again returns the stored errors without
        touching the filesystem.

        Returns:
            Errors raised while undoing, in the order they occurred; empty
            when every command was reverted.
        """
        if self._rollback_errors is not None:
            return self._rollback_errors
        errors: list[Exception] = []
        remaining: list[tuple[int, Command]] = []
        for seq, command in reversed(self._executed):
            try:
                command.undo()
                self._log.mark_command(seq=seq, status=CommandStatus.UNDONE)
            except PipelineError as error:
                errors.append(error)
                remaining.append((seq, command))
        self._executed = remaining[::-1]
        self._rollback_errors = tuple(errors)
        self._finished = True
        self._log.finish_session(
            session_id=self.session_id,
            status=SessionStatus.FAILED if errors else SessionStatus.ROLLED_BACK,
        )
        return self._rollback_errors

    def __enter__(self) -> Self:
        """Return the session for use in a ``with`` block.

        Returns:
            This session.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Commit on a clean exit, roll back on an exception.

        Args:
            exc_type: Escaping exception type, if any.
            exc_value: Escaping exception value, if any.
            traceback: Escaping exception traceback, if any.

        Returns:
            ``False`` so the block's exception is never suppressed; undo
            failures are attached to it with ``add_note``.
        """
        del traceback
        if self._finished:
            return False
        if exc_type is None:
            self.commit()
            return False
        errors = self.rollback()
        if errors:
            _LOGGER.warning(
                "saga session %s rollback left %d command(s) unreverted",
                self.session_id,
                len(errors),
            )
            if exc_value is not None:
                for error in errors:
                    exc_value.add_note(f"rollback failed: {error}")
        return False

    def _require_open(self, operation: str) -> None:
        """Fail when the session has already been committed or rolled back.

        Args:
            operation: Calling method name, used for error context.

        Raises:
            SagaError: When the session is finished.
        """
        if self._finished:
            raise SagaError(
                "saga session has already finished",
                operation=f"saga.session.{operation}",
                details={"session_id": self.session_id},
            )


__all__ = ["SagaSession"]
