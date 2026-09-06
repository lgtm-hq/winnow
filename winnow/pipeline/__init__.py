"""Winnow pipeline package: reversible commands, run context, and step contract.

Exposes the command pattern implementations used for reversible file mutations,
the :class:`PipelineContext` dependency-injection container that wires services
into pipeline steps, and the step contract (:class:`Step`, :class:`RunState`,
:class:`StepEvents` and its event types) every step builds on.
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
from winnow.pipeline.state import RunState
from winnow.pipeline.step import Step
from winnow.pipeline.steps import DiscoveryStep

__all__ = [
    "Command",
    "CopyFile",
    "CreateDirectory",
    "DeleteFile",
    "MoveFile",
    "NullEvents",
    "PipelineContext",
    "PipelineEvent",
    "RunState",
    "Step",
    "StepCompleted",
    "StepEvents",
    "StepIssue",
    "StepProgress",
    "StepStarted",
    "DiscoveryStep",
]
