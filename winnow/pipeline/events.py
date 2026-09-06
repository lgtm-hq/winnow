"""Progress events emitted by pipeline steps.

Steps report their lifecycle through a :class:`StepEvents` sink so that adapters
(CLI, API, tests) can observe progress without the steps importing any output
library. :class:`NullEvents` is the default sink and discards everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from winnow.models.pipeline import PipelineStep


@dataclass(frozen=True, slots=True)
class StepStarted:
    """A step has begun running.

    Args:
        step: The step that started.
    """

    step: PipelineStep


@dataclass(frozen=True, slots=True)
class StepProgress:
    """A step has made measurable progress.

    Args:
        step: The step reporting progress.
        current: Number of units processed so far.
        total: Total units expected, or ``None`` when unknown.
        path: Path currently being processed, when applicable.
    """

    step: PipelineStep
    current: int
    total: int | None = None
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class StepCompleted:
    """A step has finished running.

    Args:
        step: The step that completed.
        duration_seconds: Wall-clock time the step took.
    """

    step: PipelineStep
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class StepIssue:
    """A step encountered a non-fatal problem and continued.

    Args:
        step: The step that hit the problem.
        message: Human-readable description of the problem.
        path: Path the problem relates to, when applicable.
    """

    step: PipelineStep
    message: str
    path: Path | None = None


PipelineEvent = StepStarted | StepProgress | StepCompleted | StepIssue


class StepEvents(Protocol):
    """Sink that receives pipeline events as steps run."""

    def emit(self, event: PipelineEvent) -> None:
        """Deliver one event to the sink.

        Args:
            event: The event to deliver.
        """


class NullEvents:
    """Event sink that discards every event."""

    def emit(self, event: PipelineEvent) -> None:
        """Discard the event.

        Args:
            event: The event to discard.
        """


__all__ = [
    "NullEvents",
    "PipelineEvent",
    "StepCompleted",
    "StepEvents",
    "StepIssue",
    "StepProgress",
    "StepStarted",
]
