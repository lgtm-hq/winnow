"""Public media helpers."""

from __future__ import annotations

from winnow.media.audio import extract_audio_metadata, read_audio_tags
from winnow.media.image import (
    DEFAULT_THUMBNAIL_SIZE,
    extract_image_metadata,
    generate_thumbnail,
    heif_supported,
    read_exif,
)
from winnow.media.registry import (
    DEFAULT_FORMAT_REGISTRY,
    DEFAULT_FORMATS,
    RAW_IMAGE_MIME_TYPES,
    FormatRegistry,
    create_default_format_registry,
    detect_media_type,
    media_type_for_extension,
    normalize_extension,
)
from winnow.media.video import (
    extract_frame,
    extract_video_metadata,
    ffmpeg_available,
    ffprobe_available,
)

__all__ = [
    "DEFAULT_FORMATS",
    "DEFAULT_FORMAT_REGISTRY",
    "DEFAULT_THUMBNAIL_SIZE",
    "RAW_IMAGE_MIME_TYPES",
    "FormatRegistry",
    "create_default_format_registry",
    "detect_media_type",
    "extract_audio_metadata",
    "extract_frame",
    "extract_image_metadata",
    "extract_video_metadata",
    "ffmpeg_available",
    "ffprobe_available",
    "generate_thumbnail",
    "heif_supported",
    "media_type_for_extension",
    "normalize_extension",
    "read_audio_tags",
    "read_exif",
]
