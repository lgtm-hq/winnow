"""Image content classification.

This package groups heuristic classifiers used during scanning:

- Screenshot detection (:mod:`winnow.classify.screenshot`).
- Photograph vs. graphic classification (:mod:`winnow.classify.photo_graphic`).
- Special-folder route resolution (:mod:`winnow.classify.routing`), which maps
  those results plus the Live Photo signal to a configured folder name.

Each classifier exposes a pure, standard-library function that scores
pre-extracted signals, plus a Pillow-backed convenience wrapper that reads those
signals from an image file.
"""

from __future__ import annotations

from winnow.classify._image import ColorCounts
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
    "DEFAULT_CAMERA_EXIF_TAGS",
    "DEFAULT_DISPLAY_ASPECT_RATIOS",
    "DEFAULT_SCREENSHOT_SOFTWARE_MARKERS",
    "SCREENSHOT_FILENAME_PATTERNS",
    "ColorCounts",
    "FileClassification",
    "ImageContentType",
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
    "detect_photo_or_graphic",
    "detect_screenshot",
    "plan_destination_dir",
    "resolve_route",
]
