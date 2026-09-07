"""Winnow pipeline package: reversible commands, session log, run context, steps.

Exposes the command pattern implementations used for reversible file mutations,
the durable :class:`SagaLog` that records them, the :class:`PipelineContext`
dependency-injection container that wires services into pipeline steps, the
step contract (:class:`Step`, :class:`RunState`, :class:`StepEvents` and its
event types) every step builds on, and the :class:`EventBus` fan-out sink that
adapters subscribe to.
"""

from __future__ import annotations

from winnow.pipeline.bus import EventBus, HandlerError
from winnow.pipeline.commands import (
    Command,
    CopyFile,
    CreateDirectory,
    DeleteFile,
    MoveFile,
)
from winnow.pipeline.context import PipelineContext
from winnow.pipeline.events import (
    DuplicateFound,
    FileMoved,
    NullEvents,
    PipelineEvent,
    StepCompleted,
    StepEvents,
    StepIssue,
    StepProgress,
    StepStarted,
)
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
from winnow.pipeline.steps import DiscoveryStep

__all__ = [
    "Command",
    "CommandRecord",
    "CommandStatus",
    "CopyFile",
    "CreateDirectory",
    "DeleteFile",
    "DiscoveryStep",
    "DuplicateFound",
    "EventBus",
    "FileMoved",
    "HandlerError",
    "MoveFile",
    "NullEvents",
    "PipelineContext",
    "PipelineEvent",
    "RunState",
    "SagaLog",
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
