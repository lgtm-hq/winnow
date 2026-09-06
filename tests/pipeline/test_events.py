"""Tests for pipeline step events and the null event sink."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.models.pipeline import PipelineStep
from winnow.pipeline import (
    NullEvents,
    PipelineEvent,
    StepCompleted,
    StepEvents,
    StepIssue,
    StepProgress,
    StepStarted,
)


def test_null_events_emit_is_a_no_op() -> None:
    """NullEvents.emit accepts any event without raising or storing it."""
    sink = NullEvents()

    sink.emit(StepStarted(step=PipelineStep.DISCOVERY))
    sink.emit(StepIssue(step=PipelineStep.DISCOVERY, message="boom"))

    assert_that(vars(sink)).is_empty()


def test_null_events_satisfies_step_events_protocol() -> None:
    """NullEvents can be used wherever a StepEvents sink is expected."""
    sink: StepEvents = NullEvents()

    assert_that(sink).is_instance_of(NullEvents)


@pytest.mark.parametrize(
    "event",
    [
        StepStarted(step=PipelineStep.DISCOVERY),
        StepProgress(step=PipelineStep.DISCOVERY, current=1),
        StepCompleted(step=PipelineStep.DISCOVERY, duration_seconds=0.5),
        StepIssue(step=PipelineStep.DISCOVERY, message="boom"),
    ],
    ids=["started", "progress", "completed", "issue"],
)
def test_events_are_frozen(event: PipelineEvent) -> None:
    """Event dataclasses reject attribute assignment after construction."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(event, "step", PipelineStep.SCAN)  # noqa: B010 - frozen check


@pytest.mark.parametrize(
    "event",
    [
        StepStarted(step=PipelineStep.DISCOVERY),
        StepProgress(step=PipelineStep.DISCOVERY, current=1),
        StepCompleted(step=PipelineStep.DISCOVERY, duration_seconds=0.5),
        StepIssue(step=PipelineStep.DISCOVERY, message="boom"),
    ],
    ids=["started", "progress", "completed", "issue"],
)
def test_events_use_slots(event: PipelineEvent) -> None:
    """Event dataclasses declare __slots__ and have no instance dict."""
    assert_that(type(event)).has___slots__(tuple(type(event).__slots__))
    assert_that(hasattr(event, "__dict__")).is_false()


def test_step_progress_optional_fields_default_to_none() -> None:
    """StepProgress leaves total and path unset unless provided."""
    event = StepProgress(step=PipelineStep.DISCOVERY, current=3)

    assert_that(event.total).is_none()
    assert_that(event.path).is_none()


def test_step_issue_carries_path() -> None:
    """StepIssue stores the optional path it relates to."""
    path = Path("/media/photo.jpg")

    event = StepIssue(step=PipelineStep.DISCOVERY, message="unreadable", path=path)

    assert_that(event.path).is_equal_to(path)
    assert_that(event.message).is_equal_to("unreadable")
