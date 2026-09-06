"""Record types and status enums persisted by the saga session log.

The names and fields here are a contract shared with the ``history``/``undo``
commands (#56) and the ``stage`` command (#58); change them only together with
those consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto


class SessionStatus(StrEnum):
    """Lifecycle states of a saga session."""

    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    ROLLED_BACK = auto()
    INTERRUPTED = auto()


class CommandStatus(StrEnum):
    """Lifecycle states of a command recorded within a session."""

    IN_PROGRESS = auto()
    DONE = auto()
    UNDONE = auto()
    FAILED = auto()
    INTERRUPTED = auto()


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One row of the ``sessions`` table.

    Attributes:
        session_id: Unique session identifier.
        started_at: ISO-8601 UTC timestamp when the session began.
        completed_at: ISO-8601 UTC timestamp when it finished, if it has.
        status: Current lifecycle state.
        command_count: Number of commands recorded for the session.
        config_digest: Digest of the configuration the session ran with.
        source: Source directory the session operated on.
        destination: Destination directory the session wrote to.
    """

    session_id: str
    started_at: str
    completed_at: str | None
    status: SessionStatus
    command_count: int
    config_digest: str
    source: str
    destination: str


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """One row of the ``commands`` table.

    Attributes:
        seq: Monotonic sequence number, unique across all sessions.
        session_id: Session the command belongs to.
        command_type: ``Command.command_name`` of the recorded command.
        args: Serialized command as produced by ``Command.to_dict()``.
        log: Serialized ``OperationLog`` once the command completed, else
            ``None``.
        status: Current lifecycle state.
        timestamp: ISO-8601 UTC timestamp when the command was recorded.
        completed_at: ISO-8601 UTC timestamp of the last status change, if any.
    """

    seq: int
    session_id: str
    command_type: str
    args: dict[str, object]
    log: dict[str, object] | None
    status: CommandStatus
    timestamp: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class UndoReport:
    """Outcome of undoing a recorded session.

    Attributes:
        session_id: Session that was (or would be) undone.
        planned: Commands selected for reversal, newest first.
        reverted: Number of commands successfully undone.
        skipped: Commands left untouched, each paired with the reason.
    """

    session_id: str
    planned: list[CommandRecord]
    reverted: int
    skipped: list[tuple[CommandRecord, str]]


__all__ = [
    "CommandRecord",
    "CommandStatus",
    "SessionRecord",
    "SessionStatus",
    "UndoReport",
]
