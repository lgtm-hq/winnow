"""Winnow pipeline package: reversible commands, session log and run context.

Exposes the command pattern implementations used for reversible file mutations,
the durable :class:`SagaLog` that records them, and the :class:`PipelineContext`
dependency-injection container that wires services into pipeline steps.
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
from winnow.pipeline.saga_log import SagaLog
from winnow.pipeline.saga_records import (
    CommandRecord,
    CommandStatus,
    SessionRecord,
    SessionStatus,
    UndoReport,
)

__all__ = [
    "Command",
    "CommandRecord",
    "CommandStatus",
    "CopyFile",
    "CreateDirectory",
    "DeleteFile",
    "MoveFile",
    "PipelineContext",
    "SagaLog",
    "SessionRecord",
    "SessionStatus",
    "UndoReport",
]
