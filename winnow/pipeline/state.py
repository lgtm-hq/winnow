"""Mutable per-run state shared by pipeline steps.

:class:`RunState` carries the data a run accumulates as steps execute: the
source and destination roots, the discovered files, the aggregate result, and
the event sink steps report through. Services live in
:class:`~winnow.pipeline.context.PipelineContext`; run data lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from winnow.models.media import MediaFile
from winnow.models.pipeline import PipelineResult, PipelineStep
from winnow.pipeline.events import NullEvents, StepEvents, StepIssue


@dataclass(slots=True)
class RunState:
    """Mutable state threaded through every step of one pipeline run.

    Args:
        source: Root directory the run reads from.
        destination: Root directory the run writes to.
        result: Aggregate result the steps fill in.
        files: Media files discovered so far, in discovery order.
        events: Sink that receives progress and issue events.
        step_durations: Wall-clock seconds each completed step took.
    """

    source: Path
    destination: Path
    result: PipelineResult
    files: list[MediaFile] = field(default_factory=list)
    events: StepEvents = field(default_factory=NullEvents)
    step_durations: dict[PipelineStep, float] = field(default_factory=dict)

    def record_issue(
        self,
        *,
        step: PipelineStep,
        message: str,
        path: Path | None = None,
    ) -> None:
        """Append a non-fatal issue to ``result.errors`` and emit a StepIssue.

        The recorded string is ``"{step}: {path}: {message}"``, or
        ``"{step}: {message}"`` when ``path`` is ``None``.

        Args:
            step: The step reporting the issue.
            message: Human-readable description of the issue.
            path: Path the issue relates to, when applicable.
        """
        if path is None:
            entry = f"{step.value}: {message}"
        else:
            entry = f"{step.value}: {path}: {message}"
        self.result.errors.append(entry)
        self.events.emit(StepIssue(step=step, message=message, path=path))


__all__ = ["RunState"]
