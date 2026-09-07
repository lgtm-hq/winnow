"""Plugin registry and built-in plugins for pipeline runs.

A :class:`FeaturePlugin` is any object with a ``name``, a tuple of dependency
names, and a ``setup`` hook that subscribes to the run's :class:`EventBus`.
:class:`PluginRegistry` collects plugins by explicit registration and
initializes them once, in dependency order, so optional capabilities plug into
the pipeline without editing core steps. Two built-ins ship here because they
need no adapter code: :class:`LoggingPlugin` mirrors events to loguru and
:class:`ProgressPlugin` keeps plain counters that CLI and API renderers read.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from winnow.exceptions import PipelineError
from winnow.models.enums import MoveKind
from winnow.models.pipeline import PipelineStep
from winnow.pipeline.bus import EventBus
from winnow.pipeline.events import (
    DuplicateFound,
    FileMoved,
    PipelineEvent,
    StepCompleted,
    StepIssue,
    StepProgress,
    StepStarted,
)

if TYPE_CHECKING:
    from winnow.pipeline.context import PipelineContext


class FeaturePlugin(Protocol):
    """Optional capability that hooks into a pipeline run via the event bus."""

    @property
    def name(self) -> str:
        """Unique plugin name used for registration and dependencies."""
        ...

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Names of plugins that must be initialized before this one."""
        ...

    def setup(self, *, context: PipelineContext, bus: EventBus) -> None:
        """Wire the plugin into a run.

        Args:
            context: Service container for the run.
            bus: Event bus the plugin subscribes to.
        """


class PluginRegistry:
    """Explicit plugin registry that initializes plugins in dependency order.

    Plugins are registered by name and initialized once with Kahn's algorithm
    over their declared dependencies; ties are broken by registration order so
    the result is deterministic. The registry owns the :class:`EventBus` that
    plugins subscribe to.
    """

    def __init__(self, *, bus: EventBus | None = None) -> None:
        self._bus = bus if bus is not None else EventBus()
        self._plugins: dict[str, FeaturePlugin] = {}
        self._initialized: tuple[str, ...] | None = None

    @property
    def bus(self) -> EventBus:
        """Event bus handed to every plugin's ``setup``."""
        return self._bus

    @property
    def initialized(self) -> tuple[str, ...]:
        """Plugin names in initialization order; empty before ``initialize``."""
        return self._initialized or ()

    def register(self, plugin: FeaturePlugin) -> None:
        """Add a plugin to the registry.

        Args:
            plugin: The plugin to register.

        Raises:
            PipelineError: When a plugin with the same name is already
                registered.
        """
        if plugin.name in self._plugins:
            raise PipelineError(
                f"plugin '{plugin.name}' is already registered",
                operation="pipeline.plugins.register",
                details={"plugin": plugin.name},
            )
        self._plugins[plugin.name] = plugin

    def plugin(self, name: str) -> FeaturePlugin:
        """Return a registered plugin by name.

        Args:
            name: Name the plugin was registered under.

        Returns:
            The registered plugin.

        Raises:
            PipelineError: When no plugin with that name is registered.
        """
        found = self._plugins.get(name)
        if found is None:
            raise PipelineError(
                f"plugin '{name}' is not registered",
                operation="pipeline.plugins.plugin",
                details={"plugin": name},
            )
        return found

    def initialize(self, *, context: PipelineContext) -> tuple[str, ...]:
        """Call ``setup`` on every plugin in dependency order, once.

        A second call returns the order from the first call and invokes
        nothing.

        Args:
            context: Service container passed to each plugin's ``setup``.

        Returns:
            Plugin names in the order they were initialized.

        Raises:
            PipelineError: When a dependency is missing or the dependency
                graph contains a cycle.
        """
        if self._initialized is not None:
            return self._initialized
        order = self._topological_order()
        for name in order:
            self._plugins[name].setup(context=context, bus=self._bus)
        self._initialized = order
        return order

    def _topological_order(self) -> tuple[str, ...]:
        """Order plugins with Kahn's algorithm, registration order as tie-break.

        Returns:
            Plugin names such that every plugin follows its dependencies.

        Raises:
            PipelineError: When a dependency is missing or a cycle exists.
        """
        indegree = {name: 0 for name in self._plugins}
        dependents: dict[str, list[str]] = {name: [] for name in self._plugins}
        for name, plugin in self._plugins.items():
            for dependency in plugin.dependencies:
                if dependency not in self._plugins:
                    raise PipelineError(
                        f"plugin '{name}' depends on unknown plugin '{dependency}'",
                        operation="pipeline.plugins.initialize",
                        details={"plugin": name, "missing": dependency},
                    )
                indegree[name] += 1
                dependents[dependency].append(name)

        ready = deque(name for name, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while ready:
            name = ready.popleft()
            order.append(name)
            for dependent in dependents[name]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

        if len(order) != len(self._plugins):
            cycle = [name for name in self._plugins if name not in order]
            raise PipelineError(
                "plugin dependency cycle detected",
                operation="pipeline.plugins.initialize",
                details={"cycle": cycle},
            )
        return tuple(order)


class LoggingPlugin:
    """Built-in plugin that mirrors every pipeline event to loguru.

    Step boundaries (:class:`StepStarted`, :class:`StepCompleted`) log at info;
    everything else logs at debug.
    """

    name = "logging"
    dependencies: tuple[str, ...] = ()

    def setup(self, *, context: PipelineContext, bus: EventBus) -> None:
        """Subscribe the logger to every event on the bus.

        Args:
            context: Service container for the run (unused).
            bus: Event bus to subscribe to.
        """
        del context
        bus.subscribe_all(self._log)

    @staticmethod
    def _log(event: PipelineEvent) -> None:
        """Log one event at the level appropriate for its type.

        Args:
            event: The event to log.
        """
        level = "INFO" if isinstance(event, StepStarted | StepCompleted) else "DEBUG"
        logger.log(level, "{event}", event=event)


class ProgressPlugin:
    """Built-in plugin that keeps read-only counters for a run.

    Renderers (CLI, API) read the properties; the plugin never prints.
    """

    name = "progress"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._current_step: PipelineStep | None = None
        self._files_discovered = 0
        self._files_moved = 0
        self._duplicates_found = 0
        self._issues = 0

    @property
    def current_step(self) -> PipelineStep | None:
        """Step most recently started, or ``None`` before the first step."""
        return self._current_step

    @property
    def files_discovered(self) -> int:
        """Highest ``StepProgress.current`` seen for the discovery step."""
        return self._files_discovered

    @property
    def files_moved(self) -> int:
        """Number of :class:`FileMoved` events with kind ``DATED``."""
        return self._files_moved

    @property
    def duplicates_found(self) -> int:
        """Number of :class:`DuplicateFound` events seen."""
        return self._duplicates_found

    @property
    def issues(self) -> int:
        """Number of :class:`StepIssue` events seen."""
        return self._issues

    def setup(self, *, context: PipelineContext, bus: EventBus) -> None:
        """Subscribe the counters to every event on the bus.

        Args:
            context: Service container for the run (unused).
            bus: Event bus to subscribe to.
        """
        del context
        bus.subscribe_all(self._on_event)

    def _on_event(self, event: PipelineEvent) -> None:
        """Update the counter that tracks this event type, if any.

        Args:
            event: The event to count.
        """
        match event:
            case StepStarted(step=step):
                self._current_step = step
            case StepProgress(step=PipelineStep.DISCOVERY, current=current):
                self._files_discovered = max(self._files_discovered, current)
            case FileMoved(kind=MoveKind.DATED):
                self._files_moved += 1
            case DuplicateFound():
                self._duplicates_found += 1
            case StepIssue():
                self._issues += 1


__all__ = ["FeaturePlugin", "LoggingPlugin", "PluginRegistry", "ProgressPlugin"]
