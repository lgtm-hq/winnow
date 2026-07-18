"""Image content classification.

This package groups heuristic classifiers used during scanning:

- Screenshot detection (:mod:`winnow.classify.screenshot`).
- Photograph vs. graphic classification (:mod:`winnow.classify.photo_graphic`).

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
from winnow.classify.screenshot import (
    COMMON_SCREEN_RESOLUTIONS,
    SCREENSHOT_FILENAME_PATTERNS,
    SCREENSHOT_SOFTWARE_MARKERS,
    ScreenshotClassification,
    ScreenshotConfig,
    ScreenshotSignals,
    classify_screenshot,
    detect_screenshot,
)

__all__ = [
    "COMMON_SCREEN_RESOLUTIONS",
    "DEFAULT_CAMERA_EXIF_TAGS",
    "SCREENSHOT_FILENAME_PATTERNS",
    "SCREENSHOT_SOFTWARE_MARKERS",
    "ColorCounts",
    "ImageContentType",
    "PhotoGraphicClassification",
    "PhotoGraphicConfig",
    "PhotoGraphicSignals",
    "ScreenshotClassification",
    "ScreenshotConfig",
    "ScreenshotSignals",
    "classify_photo_or_graphic",
    "classify_screenshot",
    "detect_photo_or_graphic",
    "detect_screenshot",
]
