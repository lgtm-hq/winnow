"""Tests for the pipeline dependency-injection context."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from winnow.exceptions import PipelineError
from winnow.models.config import WinnowConfig
from winnow.pipeline import PipelineContext


def test_from_config_defaults_services_to_none() -> None:
    """A context built from config alone leaves every service slot empty."""
    config = WinnowConfig()

    context = PipelineContext.from_config(config)

    assert_that(context.config).is_same_as(config)
    assert_that(context.metadata_service).is_none()
    assert_that(context.hasher).is_none()
    assert_that(context.cache).is_none()
    assert_that(context.saga).is_none()
    assert_that(context.reporter).is_none()


def test_from_config_accepts_injected_services() -> None:
    """The factory stores injected service instances in their slots."""
    hasher = object()

    context = PipelineContext.from_config(WinnowConfig(), hasher=hasher)

    assert_that(context.hasher).is_same_as(hasher)


def test_require_returns_configured_service() -> None:
    """require returns a configured service instance."""
    cache = object()
    context = PipelineContext.from_config(WinnowConfig(), cache=cache)

    assert_that(context.require("cache")).is_same_as(cache)


def test_require_missing_service_raises() -> None:
    """require raises PipelineError when a slot is not configured."""
    context = PipelineContext.from_config(WinnowConfig())

    with pytest.raises(PipelineError) as excinfo:
        context.require("hasher")

    assert_that(str(excinfo.value)).contains("is not configured")


def test_require_unknown_slot_raises() -> None:
    """require rejects unknown service slot names."""
    context = PipelineContext.from_config(WinnowConfig())

    with pytest.raises(PipelineError) as excinfo:
        context.require("teleporter")

    assert_that(str(excinfo.value)).contains("unknown pipeline service slot")


def test_with_services_overrides_selected_slots() -> None:
    """with_services returns a new context with overridden slots only."""
    context = PipelineContext.from_config(WinnowConfig())
    reporter = object()

    updated = context.with_services(reporter=reporter)

    assert_that(updated.reporter).is_same_as(reporter)
    assert_that(context.reporter).is_none()
    assert_that(updated.config).is_same_as(context.config)


def test_with_services_unknown_slot_raises() -> None:
    """with_services rejects unknown service slot names."""
    context = PipelineContext.from_config(WinnowConfig())

    with pytest.raises(PipelineError) as excinfo:
        context.with_services(teleporter=object())

    assert_that(str(excinfo.value)).contains("unknown pipeline service slot")


def test_context_is_frozen() -> None:
    """The context is immutable to prevent accidental mutation during a run."""
    context = PipelineContext.from_config(WinnowConfig())

    with pytest.raises(AttributeError):
        context.hasher = object()  # type: ignore[misc]
