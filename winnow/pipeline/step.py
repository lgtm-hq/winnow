"""The pipeline step contract.

Every step implements :class:`Step`. Steps take no required constructor
arguments: services come from the :class:`~winnow.pipeline.context.PipelineContext`
and run data flows through the :class:`~winnow.pipeline.state.RunState`. A step
raises :class:`~winnow.exceptions.PipelineError` only for fatal conditions;
per-file problems go through :meth:`RunState.record_issue`.
"""

from __future__ import annotations

from typing import Protocol

from winnow.models.pipeline import PipelineStep
from winnow.pipeline.context import PipelineContext
from winnow.pipeline.state import RunState


class Step(Protocol):
    """One unit of work in a pipeline run."""

    @property
    def name(self) -> PipelineStep:
        """Return the pipeline step this implementation performs.

        Returns:
            The step identifier.
        """
        ...

    def run(self, state: RunState, *, context: PipelineContext) -> None:
        """Execute the step, mutating ``state`` in place.

        Args:
            state: Mutable run state to read from and write to.
            context: Immutable service container for the run.
        """


__all__ = ["Step"]
