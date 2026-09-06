"""Tests for the Step protocol."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from assertpy import assert_that

from winnow.models.config import WinnowConfig
from winnow.models.pipeline import PipelineResult, PipelineStep, RunMetadata
from winnow.pipeline import PipelineContext, RunState, Step


class _MarkerStep:
    """Minimal Step implementation that records the state it was run with.

    Attributes:
        seen: The state passed to the last :meth:`run` call, if any.
    """

    def __init__(self) -> None:
        self.seen: RunState | None = None

    @property
    def name(self) -> PipelineStep:
        """Return the step identifier.

        Returns:
            Always ``PipelineStep.DISCOVERY``.
        """
        return PipelineStep.DISCOVERY

    def run(self, state: RunState, *, context: PipelineContext) -> None:
        """Record the state and mark the step completed.

        Args:
            state: Mutable run state.
            context: Service container for the run.
        """
        self.seen = state
        state.result.steps_completed.append(self.name)


def test_step_protocol_accepts_structural_implementation(tmp_path: Path) -> None:
    """A class with ``name`` and ``run`` satisfies Step without inheriting."""
    step: Step = _MarkerStep()
    state = RunState(
        source=tmp_path,
        destination=tmp_path,
        result=PipelineResult(
            run=RunMetadata(started_at=datetime.now(tz=UTC), winnow_version="0.0.0"),
        ),
    )

    step.run(state, context=PipelineContext.from_config(WinnowConfig()))

    assert_that(step.name).is_equal_to(PipelineStep.DISCOVERY)
    assert_that(state.result.steps_completed).is_equal_to([PipelineStep.DISCOVERY])
