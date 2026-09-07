"""Progress events emitted by pipeline steps.

Steps report their lifecycle through a :class:`StepEvents` sink so that adapters
(CLI, API, tests) can observe progress without the steps importing any output
library. :class:`NullEvents` is the default sink and discards everything; the
fan-out sink lives in :mod:`winnow.pipeline.bus`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from winnow.models.enums import MoveKind
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


@dataclass(frozen=True, slots=True)
class FileMoved:
    """A step moved one file.

    Args:
        step: The step that performed the move.
        source: Path the file was moved from.
        destination: Path the file was moved to.
        kind: Why the file was moved.
    """

    step: PipelineStep
    source: Path
    destination: Path
    kind: MoveKind


@dataclass(frozen=True, slots=True)
class DuplicateFound:
    """A step resolved one duplicate group.

    Args:
        step: The step that found the group.
        group_number: 1-based ordinal of the group within the run.
        files: Every file in the group, including ``best``.
        best: The file kept in place as the group's best copy.
    """

    step: PipelineStep
    group_number: int
    files: tuple[Path, ...]
    best: Path


PipelineEvent = (
    StepStarted | StepProgress | StepCompleted | StepIssue | FileMoved | DuplicateFound
)


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
    "DuplicateFound",
    "FileMoved",
    "NullEvents",
    "PipelineEvent",
    "StepCompleted",
    "StepEvents",
    "StepIssue",
    "StepProgress",
    "StepStarted",
]
