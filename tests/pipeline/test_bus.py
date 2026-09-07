"""Tests for the in-process pipeline event bus."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import get_args

import pytest
from assertpy import assert_that
from loguru import logger

from winnow.models.enums import MoveKind
from winnow.models.pipeline import PipelineStep
from winnow.pipeline import (
    DuplicateFound,
    EventBus,
    FileMoved,
    HandlerError,
    PipelineEvent,
    StepEvents,
    StepProgress,
    StepStarted,
)

_STARTED = StepStarted(step=PipelineStep.DISCOVERY)
_PROGRESS = StepProgress(step=PipelineStep.DISCOVERY, current=1)
_MOVED = FileMoved(
    step=PipelineStep.SCAN,
    source=Path("/in/a.jpg"),
    destination=Path("/out/2024/01-January/a.jpg"),
    kind=MoveKind.DATED,
)
_DUPLICATE = DuplicateFound(
    step=PipelineStep.DEDUPLICATION,
    group_number=1,
    files=(Path("/out/a.jpg"), Path("/out/b.jpg")),
    best=Path("/out/a.jpg"),
)


def test_event_bus_is_a_step_events_sink() -> None:
    """EventBus satisfies the StepEvents protocol structurally."""
    sink: StepEvents = EventBus()

    sink.emit(_STARTED)

    assert_that(sink).is_instance_of(EventBus)


def test_typed_handlers_fire_in_subscription_order() -> None:
    """Handlers for one event type run in the order they subscribed."""
    bus = EventBus()
    calls: list[str] = []
    bus.subscribe(StepStarted, lambda _event: calls.append("first"))
    bus.subscribe(StepStarted, lambda _event: calls.append("second"))

    bus.emit(_STARTED)

    assert_that(calls).is_equal_to(["first", "second"])


def test_typed_handlers_only_receive_their_exact_type() -> None:
    """A StepProgress handler does not fire for StepStarted."""
    bus = EventBus()
    received: list[PipelineEvent] = []
    bus.subscribe(StepProgress, received.append)

    bus.emit(_STARTED)
    bus.emit(_PROGRESS)

    assert_that(received).is_equal_to([_PROGRESS])


def test_subscribe_all_receives_every_event_after_typed_handlers() -> None:
    """Catch-all handlers see each event, after the typed handlers ran."""
    bus = EventBus()
    order: list[str] = []
    bus.subscribe_all(lambda event: order.append(f"all:{type(event).__name__}"))
    bus.subscribe(StepStarted, lambda _event: order.append("typed:StepStarted"))

    bus.emit(_STARTED)
    bus.emit(_MOVED)

    assert_that(order).is_equal_to(
        ["typed:StepStarted", "all:StepStarted", "all:FileMoved"],
    )


def test_emit_with_no_subscribers_is_a_no_op() -> None:
    """Emitting on an empty bus neither raises nor records errors."""
    bus = EventBus()

    bus.emit(_DUPLICATE)

    assert_that(bus.handler_errors).is_empty()


def _explode(_event: PipelineEvent) -> None:
    """Handler that always fails.

    Args:
        _event: Ignored.

    Raises:
        RuntimeError: Always.
    """
    msg = "boom"
    raise RuntimeError(msg)


def test_handler_exception_is_isolated_and_recorded() -> None:
    """A raising handler is skipped, logged, and recorded; later handlers run."""
    bus = EventBus()
    calls: list[str] = []
    records: list[str] = []
    sink_id = logger.add(records.append, level="WARNING")
    bus.subscribe(StepStarted, _explode)
    bus.subscribe(StepStarted, lambda _event: calls.append("after"))

    try:
        bus.emit(_STARTED)
    finally:
        logger.remove(sink_id)

    assert_that(calls).is_equal_to(["after"])
    assert_that(bus.handler_errors).is_length(1)
    failure = bus.handler_errors[0]
    assert_that(failure).is_instance_of(HandlerError)
    assert_that(failure.handler).is_equal_to(_explode.__qualname__)
    assert_that(failure.event).is_equal_to(_STARTED)
    assert_that(failure.error).is_instance_of(RuntimeError)
    assert_that(records).is_length(1)
    assert_that(records[0]).contains(_explode.__qualname__, "StepStarted", "boom")


class _HostileHandler:
    """Callable instance (no ``__qualname__``) whose ``repr`` raises."""

    def __call__(self, _event: object) -> None:
        raise RuntimeError("boom")

    def __repr__(self) -> str:
        raise ValueError("no repr for you")


def test_handler_name_fallback_never_raises() -> None:
    """A handler whose diagnostics raise still cannot interrupt delivery."""
    bus = EventBus()
    calls: list[str] = []
    sink_id = logger.add(lambda _msg: None, level="WARNING")
    bus.subscribe(StepStarted, _HostileHandler())
    bus.subscribe(StepStarted, lambda _event: calls.append("after"))

    try:
        bus.emit(_STARTED)
    finally:
        logger.remove(sink_id)

    assert_that(calls).is_equal_to(["after"])
    assert_that(bus.handler_errors).is_length(1)
    assert_that(bus.handler_errors[0].handler).is_equal_to("_HostileHandler")


def test_handler_errors_is_an_immutable_snapshot() -> None:
    """handler_errors returns a tuple that later failures do not mutate."""
    bus = EventBus()
    bus.subscribe_all(_explode)
    bus.emit(_STARTED)
    before = bus.handler_errors

    bus.emit(_PROGRESS)

    assert_that(before).is_length(1)
    assert_that(bus.handler_errors).is_length(2)


@pytest.mark.parametrize(
    "event",
    [_MOVED, _DUPLICATE],
    ids=["file_moved", "duplicate_found"],
)
def test_new_events_are_frozen_and_part_of_pipeline_event(
    event: PipelineEvent,
) -> None:
    """FileMoved and DuplicateFound reject mutation and belong to the union."""
    assert_that(get_args(PipelineEvent)).contains(type(event))
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(event, "step", PipelineStep.SCAN)  # noqa: B010 - frozen check


def test_handler_error_is_frozen() -> None:
    """HandlerError rejects attribute assignment after construction."""
    failure = HandlerError(event=_STARTED, handler="h", error=RuntimeError("x"))

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(failure, "handler", "other")  # noqa: B010 - frozen check
