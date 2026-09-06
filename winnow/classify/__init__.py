"""Image content classification.

This package groups heuristic classifiers used during scanning:

- Screenshot detection (:mod:`winnow.classify.screenshot`).
- Photograph vs. graphic classification (:mod:`winnow.classify.photo_graphic`).
- Apple Live Photo pair detection (:mod:`winnow.classify.livephoto`).
- Special-folder route resolution (:mod:`winnow.classify.routing`), which maps
  those results plus the Live Photo signal to a configured folder name.

Each classifier exposes a pure, standard-library function that scores
pre-extracted signals, plus a Pillow-backed convenience wrapper that reads those
signals from an image file.
"""

from __future__ import annotations

from winnow.classify._image import ColorCounts
from winnow.classify.livephoto import (
    APPLE_CONTENT_IDENTIFIER_TAG,
    QUICKTIME_CONTENT_IDENTIFIER_KEY,
    STILL_SUFFIXES,
    VIDEO_SUFFIXES,
    IdentifierReader,
    LivePhotoPair,
    LivePhotoScan,
    detect_live_photos,
    find_live_photo_pairs,
    still_content_identifier,
    video_content_identifier,
)
from winnow.classify.photo_graphic import (
    DEFAULT_CAMERA_EXIF_TAGS,
    ImageContentType,
    PhotoGraphicClassification,
    PhotoGraphicConfig,
    PhotoGraphicSignals,
    classify_photo_or_graphic,
    detect_photo_or_graphic,
)
from winnow.classify.routing import (
    FileClassification,
    RouteDecision,
    classify_image,
    plan_destination_dir,
    resolve_route,
)
from winnow.classify.screenshot import (
    DEFAULT_DISPLAY_ASPECT_RATIOS,
    DEFAULT_SCREENSHOT_SOFTWARE_MARKERS,
    SCREENSHOT_FILENAME_PATTERNS,
    ScreenshotClassification,
    ScreenshotConfig,
    ScreenshotSignals,
    classify_screenshot,
    detect_screenshot,
)

__all__ = [
    "APPLE_CONTENT_IDENTIFIER_TAG",
    "DEFAULT_CAMERA_EXIF_TAGS",
    "DEFAULT_DISPLAY_ASPECT_RATIOS",
    "DEFAULT_SCREENSHOT_SOFTWARE_MARKERS",
    "QUICKTIME_CONTENT_IDENTIFIER_KEY",
    "SCREENSHOT_FILENAME_PATTERNS",
    "STILL_SUFFIXES",
    "VIDEO_SUFFIXES",
    "ColorCounts",
    "FileClassification",
    "IdentifierReader",
    "ImageContentType",
    "LivePhotoPair",
    "LivePhotoScan",
    "PhotoGraphicClassification",
    "PhotoGraphicConfig",
    "PhotoGraphicSignals",
    "RouteDecision",
    "ScreenshotClassification",
    "ScreenshotConfig",
    "ScreenshotSignals",
    "classify_image",
    "classify_photo_or_graphic",
    "classify_screenshot",
    "detect_live_photos",
    "detect_photo_or_graphic",
    "detect_screenshot",
    "find_live_photo_pairs",
    "plan_destination_dir",
    "resolve_route",
    "still_content_identifier",
    "video_content_identifier",
]
