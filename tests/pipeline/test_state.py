"""Tests for the mutable per-run pipeline state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.models.pipeline import PipelineResult, PipelineStep, RunMetadata
from winnow.pipeline import NullEvents, PipelineEvent, RunState, StepIssue


class _RecordingEvents:
    """StepEvents fake that records every emitted event.

    Attributes:
        events: Every event passed to :meth:`emit`, in order.
    """

    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    def emit(self, event: PipelineEvent) -> None:
        """Append the event to the log.

        Args:
            event: The event to record.
        """
        self.events.append(event)


@pytest.fixture
def result() -> PipelineResult:
    """Return an empty pipeline result for a fresh run."""
    return PipelineResult(
        run=RunMetadata(started_at=datetime.now(tz=UTC), winnow_version="0.0.0"),
    )


def test_run_state_defaults(result: PipelineResult, tmp_path: Path) -> None:
    """RunState starts with no files, no durations and a null event sink."""
    state = RunState(source=tmp_path, destination=tmp_path / "out", result=result)

    assert_that(state.files).is_empty()
    assert_that(state.step_durations).is_empty()
    assert_that(state.events).is_instance_of(NullEvents)


def test_record_issue_with_path_formats_entry(
    result: PipelineResult,
    tmp_path: Path,
) -> None:
    """record_issue appends ``"{step}: {path}: {message}"`` when a path is given."""
    state = RunState(source=tmp_path, destination=tmp_path, result=result)
    path = tmp_path / "broken.jpg"

    state.record_issue(step=PipelineStep.DISCOVERY, message="unreadable", path=path)

    assert_that(state.result.errors).is_equal_to([f"discovery: {path}: unreadable"])


def test_record_issue_without_path_formats_entry(
    result: PipelineResult,
    tmp_path: Path,
) -> None:
    """record_issue appends ``"{step}: {message}"`` when no path is given."""
    state = RunState(source=tmp_path, destination=tmp_path, result=result)

    state.record_issue(step=PipelineStep.SCAN, message="cache unavailable")

    assert_that(state.result.errors).is_equal_to(["scan: cache unavailable"])


def test_record_issue_emits_step_issue(
    result: PipelineResult,
    tmp_path: Path,
) -> None:
    """record_issue emits one StepIssue carrying step, message and path."""
    events = _RecordingEvents()
    state = RunState(
        source=tmp_path,
        destination=tmp_path,
        result=result,
        events=events,
    )
    path = tmp_path / "broken.jpg"

    state.record_issue(step=PipelineStep.DISCOVERY, message="unreadable", path=path)

    assert_that(events.events).is_equal_to(
        [StepIssue(step=PipelineStep.DISCOVERY, message="unreadable", path=path)],
    )


def test_record_issue_accumulates(result: PipelineResult, tmp_path: Path) -> None:
    """Repeated record_issue calls append in order without dropping entries."""
    state = RunState(source=tmp_path, destination=tmp_path, result=result)

    state.record_issue(step=PipelineStep.DISCOVERY, message="first")
    state.record_issue(step=PipelineStep.DISCOVERY, message="second")

    assert_that(state.result.errors).is_equal_to(
        ["discovery: first", "discovery: second"],
    )
