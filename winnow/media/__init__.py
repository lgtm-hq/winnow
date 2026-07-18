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
    AUDIO_FORMATS,
    DEFAULT_FORMAT_REGISTRY,
    DEFAULT_FORMATS,
    IMAGE_FORMATS,
    VIDEO_FORMATS,
    FormatRegistry,
    create_default_format_registry,
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
    "AUDIO_FORMATS",
    "DEFAULT_FORMATS",
    "DEFAULT_FORMAT_REGISTRY",
    "DEFAULT_THUMBNAIL_SIZE",
    "IMAGE_FORMATS",
    "VIDEO_FORMATS",
    "FormatRegistry",
    "create_default_format_registry",
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
