"""Tests for the plugin registry and the built-in plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from assertpy import assert_that
from loguru import logger

from winnow.exceptions import PipelineError
from winnow.models.config import WinnowConfig
from winnow.models.enums import MoveKind
from winnow.models.pipeline import PipelineStep
from winnow.pipeline import (
    DuplicateFound,
    EventBus,
    FileMoved,
    LoggingPlugin,
    PipelineContext,
    PluginRegistry,
    ProgressPlugin,
    StepCompleted,
    StepIssue,
    StepProgress,
    StepStarted,
)


@dataclass
class _FakePlugin:
    """Minimal FeaturePlugin that records every setup call."""

    name: str
    deps: tuple[str, ...] = ()
    calls: list[tuple[str, PipelineContext, EventBus]] = field(default_factory=list)

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Return the declared dependency names."""
        return self.deps

    def setup(self, *, context: PipelineContext, bus: EventBus) -> None:
        """Record the context and bus handed in."""
        self.calls.append((self.name, context, bus))


@pytest.fixture
def context() -> PipelineContext:
    """Return a bare pipeline context."""
    return PipelineContext.from_config(WinnowConfig())


def test_initialize_orders_by_dependencies(context: PipelineContext) -> None:
    """Plugins registered as c, b, a initialize as a, b, c."""
    registry = PluginRegistry()
    registry.register(_FakePlugin("c", ("a", "b")))
    registry.register(_FakePlugin("b", ("a",)))
    registry.register(_FakePlugin("a"))

    order = registry.initialize(context=context)

    assert_that(order).is_equal_to(("a", "b", "c"))
    assert_that(registry.initialized).is_equal_to(("a", "b", "c"))


def test_initialize_breaks_ties_by_registration_order(
    context: PipelineContext,
) -> None:
    """Independent plugins keep registration order."""
    registry = PluginRegistry()
    for name in ("z", "m", "a"):
        registry.register(_FakePlugin(name))

    assert_that(registry.initialize(context=context)).is_equal_to(("z", "m", "a"))


def test_initialize_prefers_earlier_registered_plugin_once_ready(
    context: PipelineContext,
) -> None:
    """A dependent registered before an independent plugin runs first once ready."""
    registry = PluginRegistry()
    registry.register(_FakePlugin("a"))
    registry.register(_FakePlugin("b", ("a",)))
    registry.register(_FakePlugin("c"))

    assert_that(registry.initialize(context=context)).is_equal_to(("a", "b", "c"))


def test_register_after_initialize_raises(context: PipelineContext) -> None:
    """The registry is sealed once initialized."""
    registry = PluginRegistry()
    registry.register(_FakePlugin("a"))
    registry.initialize(context=context)

    with pytest.raises(PipelineError, match="after initialize"):
        registry.register(_FakePlugin("late"))


def test_initialize_retry_skips_already_set_up_plugins(
    context: PipelineContext,
) -> None:
    """A retried initialize does not run setup twice for earlier plugins."""

    class _FailOnce(_FakePlugin):
        failed = False

        def setup(self, *, context: PipelineContext, bus: EventBus) -> None:
            if not self.failed:
                self.failed = True
                raise PipelineError("boom", operation="test")
            super().setup(context=context, bus=bus)

    registry = PluginRegistry()
    first = _FakePlugin("a")
    flaky = _FailOnce("b", ("a",))
    registry.register(first)
    registry.register(flaky)

    with pytest.raises(PipelineError, match="boom"):
        registry.initialize(context=context)
    order = registry.initialize(context=context)

    assert_that(order).is_equal_to(("a", "b"))
    assert_that(first.calls).is_length(1)
    assert_that(flaky.calls).is_length(1)


def test_setup_receives_registry_bus_and_context(context: PipelineContext) -> None:
    """Each setup call gets the registry's bus and the supplied context."""
    bus = EventBus()
    registry = PluginRegistry(bus=bus)
    plugin = _FakePlugin("a")
    registry.register(plugin)

    registry.initialize(context=context)

    assert_that(registry.bus).is_same_as(bus)
    assert_that(plugin.calls).is_length(1)
    assert_that(plugin.calls[0][1]).is_same_as(context)
    assert_that(plugin.calls[0][2]).is_same_as(bus)


def test_registry_creates_bus_when_none_given() -> None:
    """A registry built without a bus owns a fresh EventBus."""
    assert_that(PluginRegistry().bus).is_instance_of(EventBus)


def test_cycle_raises_pipeline_error(context: PipelineContext) -> None:
    """A dependency cycle names every plugin on the cycle."""
    registry = PluginRegistry()
    registry.register(_FakePlugin("a", ("b",)))
    registry.register(_FakePlugin("b", ("a",)))

    with pytest.raises(PipelineError) as excinfo:
        registry.initialize(context=context)

    assert_that(excinfo.value.context.details["cycle"]).contains("a", "b")


def test_missing_dependency_raises_pipeline_error(context: PipelineContext) -> None:
    """An unknown dependency names both the plugin and the missing name."""
    registry = PluginRegistry()
    registry.register(_FakePlugin("a", ("ghost",)))

    with pytest.raises(PipelineError) as excinfo:
        registry.initialize(context=context)

    assert_that(excinfo.value.context.details).is_equal_to(
        {"plugin": "a", "missing": "ghost"},
    )


def test_duplicate_register_raises_pipeline_error() -> None:
    """Registering the same name twice is rejected."""
    registry = PluginRegistry()
    registry.register(_FakePlugin("a"))

    with pytest.raises(PipelineError) as excinfo:
        registry.register(_FakePlugin("a"))

    assert_that(excinfo.value.context.details).is_equal_to({"plugin": "a"})


def test_plugin_lookup() -> None:
    """plugin() returns registered plugins and rejects unknown names."""
    registry = PluginRegistry()
    plugin = _FakePlugin("a")
    registry.register(plugin)

    assert_that(registry.plugin("a")).is_same_as(plugin)
    with pytest.raises(PipelineError):
        registry.plugin("missing")


def test_initialize_is_idempotent(context: PipelineContext) -> None:
    """A second initialize returns the same order and calls no setup."""
    registry = PluginRegistry()
    plugin = _FakePlugin("a")
    registry.register(plugin)

    first = registry.initialize(context=context)
    second = registry.initialize(context=context)

    assert_that(second).is_equal_to(first)
    assert_that(plugin.calls).is_length(1)


def test_initialized_is_empty_before_initialize() -> None:
    """The initialized tuple is empty until initialize runs."""
    assert_that(PluginRegistry().initialized).is_equal_to(())


def test_builtin_plugins_count_and_log_events(context: PipelineContext) -> None:
    """LoggingPlugin logs and ProgressPlugin counts a mixed event stream."""
    registry = PluginRegistry()
    progress = ProgressPlugin()
    registry.register(LoggingPlugin())
    registry.register(progress)
    registry.initialize(context=context)
    records: list[str] = []
    sink_id = logger.add(records.append, level="DEBUG")
    bus = registry.bus
    step = PipelineStep.DISCOVERY
    src = Path("a.jpg")

    try:
        bus.emit(StepStarted(step=step))
        for current in (1, 3, 2):
            bus.emit(StepProgress(step=step, current=current, total=3))
        bus.emit(FileMoved(step=step, source=src, destination=src, kind=MoveKind.DATED))
        bus.emit(FileMoved(step=step, source=src, destination=src, kind=MoveKind.DATED))
        bus.emit(DuplicateFound(step=step, group_number=1, files=(src,), best=src))
    finally:
        logger.remove(sink_id)

    assert_that(progress.current_step).is_equal_to(step)
    assert_that(progress.files_discovered).is_equal_to(3)
    assert_that(progress.files_moved).is_equal_to(2)
    assert_that(progress.duplicates_found).is_equal_to(1)
    assert_that(progress.issues).is_equal_to(0)
    assert_that(records).is_not_empty()
    assert_that(registry.bus.handler_errors).is_empty()


def test_progress_plugin_ignores_duplicate_moves_and_counts_issues(
    context: PipelineContext,
) -> None:
    """Duplicate-kind moves do not count as files moved; issues are counted."""
    registry = PluginRegistry()
    progress = ProgressPlugin()
    registry.register(progress)
    registry.initialize(context=context)
    step = PipelineStep.DEDUPLICATION
    src = Path("a.jpg")

    registry.bus.emit(
        FileMoved(step=step, source=src, destination=src, kind=MoveKind.DUPLICATE),
    )
    registry.bus.emit(StepIssue(step=step, message="oops"))
    registry.bus.emit(StepProgress(step=step, current=9))

    assert_that(progress.files_moved).is_equal_to(0)
    assert_that(progress.issues).is_equal_to(1)
    assert_that(progress.files_discovered).is_equal_to(0)


def test_logging_plugin_logs_step_boundaries_at_info(
    context: PipelineContext,
) -> None:
    """Step start and completion log at INFO; progress logs at DEBUG."""
    registry = PluginRegistry()
    registry.register(LoggingPlugin())
    registry.initialize(context=context)
    levels: list[str] = []
    sink_id = logger.add(
        lambda message: levels.append(message.record["level"].name),
        level="DEBUG",
    )

    try:
        registry.bus.emit(StepStarted(step=PipelineStep.SCAN))
        registry.bus.emit(StepProgress(step=PipelineStep.SCAN, current=1))
        registry.bus.emit(StepCompleted(step=PipelineStep.SCAN, duration_seconds=0.1))
    finally:
        logger.remove(sink_id)

    assert_that(levels).is_equal_to(["INFO", "DEBUG", "INFO"])


def test_context_plugins_slot() -> None:
    """The context exposes the registry through require("plugins")."""
    registry = PluginRegistry()

    context = PipelineContext.from_config(WinnowConfig(), plugins=registry)

    assert_that(context.require("plugins")).is_same_as(registry)
    assert_that(PipelineContext.from_config(WinnowConfig()).plugins).is_none()
