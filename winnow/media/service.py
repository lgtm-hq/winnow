"""Unified metadata extraction service.

Consumers of media metadata (pipeline steps, caches, comparison logic) need
one entry point that classifies a file once and routes it to the matching
processor. :class:`MetadataService` is the minimal Protocol those consumers
depend on, and :class:`DefaultMetadataService` is the production
implementation that layers ``detect_media_type`` over the existing image,
video, and audio extractors.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from winnow.exceptions import MediaError
from winnow.media.audio import extract_audio_metadata
from winnow.media.image import extract_image_metadata
from winnow.media.registry import (
    FormatRegistry,
    create_default_format_registry,
    detect_media_type,
)
from winnow.media.video import extract_video_metadata
from winnow.models.media import MediaMetadata, MediaType

if TYPE_CHECKING:
    from winnow.models.config import WinnowConfig

_EXTRACTORS: Final[Mapping[MediaType, Callable[[Path], MediaMetadata]]] = {
    MediaType.IMAGE: extract_image_metadata,
    MediaType.VIDEO: extract_video_metadata,
    MediaType.AUDIO: extract_audio_metadata,
}


class MetadataService(Protocol):
    """Extract technical metadata from a media file."""

    def extract(self, path: Path) -> MediaMetadata:
        """Extract metadata for ``path``.

        Args:
            path: Media file to inspect.

        Returns:
            Extracted metadata.
        """
        ...


class DefaultMetadataService:
    """Detect a file's media type once and dispatch to its processor.

    Args:
        registry: Format registry used for detection. Defaults to Winnow's
            built-in formats.
    """

    def __init__(self, registry: FormatRegistry | None = None) -> None:
        self._registry = (
            registry if registry is not None else create_default_format_registry()
        )

    def detect(self, path: Path) -> MediaType:
        """Classify ``path`` using the configured registry.

        Args:
            path: File to classify.

        Returns:
            Detected media type.

        Raises:
            MediaError: When no detection layer can classify the file.
        """
        media_type = detect_media_type(path, registry=self._registry)
        if media_type is None:
            raise MediaError(
                "unsupported or undetectable media type",
                operation="detect_media_type",
                file_path=path,
            )
        return media_type

    def extract(self, path: Path) -> MediaMetadata:
        """Extract metadata for ``path`` via the processor for its media type.

        ``MediaError`` raised by a processor propagates unchanged; any other
        exception is wrapped in a ``MediaError`` that records the media type.

        Args:
            path: Media file to inspect.

        Returns:
            Extracted metadata. Processors may return a partially populated
            model when an optional tool such as ffprobe is unavailable.

        Raises:
            MediaError: When the path is not a regular file, is empty, cannot
                be inspected, cannot be classified, has no processor, or the
                processor fails.
        """
        try:
            is_file = path.is_file()
            size = path.stat().st_size if is_file else None
        except OSError as exc:
            raise MediaError(
                "cannot inspect file",
                operation="extract_metadata",
                file_path=path,
            ) from exc
        if not is_file:
            raise MediaError(
                "not a regular file",
                operation="extract_metadata",
                file_path=path,
            )
        if size == 0:
            raise MediaError(
                "empty file",
                operation="extract_metadata",
                file_path=path,
            )
        media_type = self.detect(path)
        extractor = _EXTRACTORS.get(media_type)
        if extractor is None:
            raise MediaError(
                "no metadata processor for media type",
                operation="extract_metadata",
                file_path=path,
                details={"media_type": media_type.value},
            )
        try:
            return extractor(path)
        except MediaError:
            raise
        except Exception as exc:
            raise MediaError(
                "metadata extraction failed",
                operation="extract_metadata",
                file_path=path,
                details={"media_type": media_type.value},
            ) from exc


def create_metadata_service(
    config: WinnowConfig | None = None,
) -> DefaultMetadataService:
    """Build the default metadata service.

    ``config`` is accepted but currently unused: ``WinnowConfig`` has no formats
    section yet. Keeping it in the signature lets the pipeline wire
    configuration-driven registries later without changing call sites.

    Args:
        config: Optional application configuration; reserved for future use.

    Returns:
        A ``DefaultMetadataService`` backed by an isolated copy of the built-in
        formats (``create_default_format_registry()``), not the process-wide
        ``DEFAULT_FORMAT_REGISTRY``; registrations made on the shared registry
        are not visible to it.
    """
    del config
    return DefaultMetadataService(registry=create_default_format_registry())


__all__ = [
    "DefaultMetadataService",
    "MetadataService",
    "create_metadata_service",
]
