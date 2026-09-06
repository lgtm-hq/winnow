"""Synchronous, ordered, in-process event bus for pipeline runs.

:class:`EventBus` is the fan-out implementation of the :class:`StepEvents`
seam defined in :mod:`winnow.pipeline.events`. Steps emit events into it and
adapters (CLI renderers, metrics, plugins) subscribe by event type or to every
event. Delivery is synchronous and ordered; a failing handler is logged and
recorded in :attr:`EventBus.handler_errors` but never interrupts the step or
the remaining handlers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from loguru import logger

from winnow.pipeline.events import PipelineEvent

E = TypeVar("E", bound=PipelineEvent)

Handler = Callable[[Any], None]


@dataclass(frozen=True, slots=True)
class HandlerError:
    """Record of a subscriber that raised while handling an event.

    Args:
        event: The event being delivered when the handler failed.
        handler: Qualified name of the failing handler.
        error: The exception the handler raised.
    """

    event: PipelineEvent
    handler: str
    error: Exception


class EventBus:
    """Ordered in-process event dispatcher; structurally a ``StepEvents``.

    Handlers registered with :meth:`subscribe` receive only events of exactly
    the subscribed type; handlers registered with :meth:`subscribe_all`
    receive every event after the typed handlers. Within each group handlers
    run in subscription order. A new bus has no subscribers.
    """

    def __init__(self) -> None:
        self._typed: dict[type[Any], list[Handler]] = defaultdict(list)
        self._catch_all: list[Handler] = []
        self._errors: list[HandlerError] = []

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], None],
    ) -> None:
        """Register a handler for one exact event type.

        Args:
            event_type: The event class to listen for.
            handler: Callable invoked with each event of that type.
        """
        self._typed[event_type].append(handler)

    def subscribe_all(self, handler: Callable[[PipelineEvent], None]) -> None:
        """Register a handler that receives every event.

        Args:
            handler: Callable invoked with each emitted event.
        """
        self._catch_all.append(handler)

    def emit(self, event: PipelineEvent) -> None:
        """Deliver an event to typed handlers, then to catch-all handlers.

        Exceptions raised by handlers are logged at warning level, recorded in
        :attr:`handler_errors`, and otherwise swallowed so that later handlers
        and the emitting step keep running.

        Args:
            event: The event to deliver.
        """
        for handler in (*self._typed.get(type(event), ()), *self._catch_all):
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - isolate subscriber failures
                name = _handler_name(handler)
                logger.warning(
                    "event handler {handler} failed on {event}: {error}",
                    handler=name,
                    event=type(event).__name__,
                    error=exc,
                )
                self._errors.append(HandlerError(event=event, handler=name, error=exc))

    @property
    def handler_errors(self) -> tuple[HandlerError, ...]:
        """Failures recorded so far, in the order they happened.

        Returns:
            One :class:`HandlerError` per handler exception.
        """
        return tuple(self._errors)


def _handler_name(handler: Handler) -> str:
    """Return a diagnostic name for a handler without ever raising.

    Args:
        handler: The callable that failed.

    Returns:
        ``__qualname__`` when present, else ``repr()``, else the type name.
    """
    qualname = getattr(handler, "__qualname__", None)
    if isinstance(qualname, str):
        return qualname
    try:
        return repr(handler)
    except Exception:  # noqa: BLE001 - diagnostics must not break delivery
        return type(handler).__name__


__all__ = ["EventBus", "HandlerError"]
