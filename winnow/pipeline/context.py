"""Dependency-injection container for winnow pipeline runs.

:class:`PipelineContext` is the composition root for a single pipeline run. It
holds the validated configuration plus service slots that steps depend on
(metadata extraction, hashing, caching, saga coordination, reporting). Services
that are not yet implemented default to ``None`` stubs so steps and tests can be
wired against a stable container today and receive real services later without
changing call sites.

Building the container in one place avoids circular imports between adapters
(CLI, API) and pipeline steps, and lets tests inject fakes without Click or
FastAPI.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any, Self, cast

from winnow.exceptions import PipelineError
from winnow.models.config import WinnowConfig

if TYPE_CHECKING:
    from winnow.media.service import MetadataService

Hasher = object
Cache = object
Saga = object
Reporter = object


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """Immutable service container passed into pipeline steps.

    Args:
        config: Validated application configuration for the run.
        metadata_service: Media metadata extractor, or ``None`` when unbuilt.
        hasher: Content and perceptual hasher, or ``None`` when unbuilt.
        cache: Hash and metadata cache, or ``None`` when unbuilt.
        saga: Transactional command coordinator, or ``None`` when unbuilt.
        reporter: Run report writer, or ``None`` when unbuilt.
    """

    config: WinnowConfig
    metadata_service: MetadataService | None = None
    hasher: Hasher | None = None
    cache: Cache | None = None
    saga: Saga | None = None
    reporter: Reporter | None = None

    @classmethod
    def from_config(
        cls,
        config: WinnowConfig,
        *,
        metadata_service: MetadataService | None = None,
        hasher: Hasher | None = None,
        cache: Cache | None = None,
        saga: Saga | None = None,
        reporter: Reporter | None = None,
    ) -> Self:
        """Build a context from configuration and optional services.

        Args:
            config: Validated application configuration for the run.
            metadata_service: Optional media metadata extractor.
            hasher: Optional content and perceptual hasher.
            cache: Optional hash and metadata cache.
            saga: Optional transactional command coordinator.
            reporter: Optional run report writer.

        Returns:
            A populated pipeline context.
        """
        return cls(
            config=config,
            metadata_service=metadata_service,
            hasher=hasher,
            cache=cache,
            saga=saga,
            reporter=reporter,
        )

    def with_services(self, **services: object | None) -> Self:
        """Return a copy of the context with selected service slots replaced.

        Args:
            **services: Service slot names mapped to replacement instances.

        Returns:
            A new context with the requested slots overridden.

        Raises:
            PipelineError: When an unknown service slot name is supplied.
        """
        unknown = set(services) - _SERVICE_SLOTS
        if unknown:
            raise PipelineError(
                "unknown pipeline service slot(s)",
                operation="pipeline.context.with_services",
                details={"unknown": sorted(unknown)},
            )
        return replace(self, **cast("dict[str, Any]", services))

    def require(self, slot: str) -> object:
        """Return a configured service, raising when the slot is empty.

        Args:
            slot: Name of the service slot to retrieve.

        Returns:
            The configured service instance.

        Raises:
            PipelineError: When the slot name is unknown or the service is not
                configured for this run.
        """
        if slot not in _SERVICE_SLOTS:
            raise PipelineError(
                "unknown pipeline service slot",
                operation="pipeline.context.require",
                details={"slot": slot},
            )
        service = getattr(self, slot)
        if service is None:
            raise PipelineError(
                f"pipeline service '{slot}' is not configured for this run",
                operation="pipeline.context.require",
                details={"slot": slot},
            )
        return service


_SERVICE_SLOTS: frozenset[str] = frozenset(
    context_field.name for context_field in fields(PipelineContext)
) - {"config"}


__all__ = ["PipelineContext"]
