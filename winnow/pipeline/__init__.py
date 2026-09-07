"""Winnow pipeline package: reversible commands, saga, run context, steps.

Exposes the command pattern implementations used for reversible file mutations,
the durable :class:`SagaLog` and the :class:`Saga` coordinator that records and
reverses them, the :class:`PipelineContext`
dependency-injection container that wires services into pipeline steps, and the
step contract (:class:`Step`, :class:`RunState`, :class:`StepEvents` and its
event types) every step builds on.
"""

from __future__ import annotations

from winnow.pipeline.commands import (
    Command,
    CopyFile,
    CreateDirectory,
    DeleteFile,
    MoveFile,
)
from winnow.pipeline.context import PipelineContext
from winnow.pipeline.events import (
    NullEvents,
    PipelineEvent,
    StepCompleted,
    StepEvents,
    StepIssue,
    StepProgress,
    StepStarted,
)
from winnow.pipeline.saga import Saga, SagaSession
from winnow.pipeline.saga_log import SagaLog
from winnow.pipeline.saga_records import (
    CommandRecord,
    CommandStatus,
    SessionRecord,
    SessionStatus,
    UndoReport,
)
from winnow.pipeline.state import RunState
from winnow.pipeline.step import Step

__all__ = [
    "Command",
    "CommandRecord",
    "CommandStatus",
    "CopyFile",
    "CreateDirectory",
    "DeleteFile",
    "MoveFile",
    "NullEvents",
    "PipelineContext",
    "PipelineEvent",
    "RunState",
    "Saga",
    "SagaLog",
    "SagaSession",
    "SessionRecord",
    "SessionStatus",
    "Step",
    "StepCompleted",
    "StepEvents",
    "StepIssue",
    "StepProgress",
    "StepStarted",
    "UndoReport",
]
