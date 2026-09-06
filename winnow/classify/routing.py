"""Special-folder route resolution for classified media.

Maps the existing classifier results (screenshot, photograph vs. graphic) plus
the Live Photo signal to a :class:`~winnow.models.enums.SpecialCategory` and
the folder name configured in :class:`~winnow.models.config.RoutingSettings`.

Resolution order (the first match wins):

1. Routing disabled -> no route.
2. Live Photo -> ``LIVE_PHOTO``.
3. Screenshot at or above ``min_confidence`` -> ``SCREENSHOT``.
4. Graphic at or above ``min_confidence`` -> ``GRAPHIC``.
5. Screenshot or graphic below ``min_confidence`` -> ``REVIEW``.
6. Otherwise -> no route (dated layout).

``PHOTO`` and ``UNKNOWN`` content never route. ``Duplicates/`` is produced by
the dedup step after execution and is never a route.

:func:`classify_image` is the only function here that touches disk;
:func:`resolve_route` and :func:`plan_destination_dir` are pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath

from winnow.classify.photo_graphic import (
    ImageContentType,
    PhotoGraphicClassification,
    PhotoGraphicConfig,
    detect_photo_or_graphic,
)
from winnow.classify.screenshot import (
    ScreenshotClassification,
    ScreenshotConfig,
    detect_screenshot,
)
from winnow.models.config import RoutingSettings
from winnow.models.enums import SpecialCategory


@dataclass(frozen=True, slots=True)
class FileClassification:
    """Classifier outputs for a single file.

    Attributes:
        screenshot: Screenshot detection result, or ``None`` when not run.
        photo_graphic: Photo vs. graphic result, or ``None`` when not run.
        live_photo: Whether the file is part of a verified Live Photo pair.
    """

    screenshot: ScreenshotClassification | None = None
    photo_graphic: PhotoGraphicClassification | None = None
    live_photo: bool = False


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Where a classified file should be placed.

    Attributes:
        category: The matched special category, or ``None`` for the dated layout.
        folder: Configured folder name for ``category``, or ``None``.
        keep_dated_layout: Whether the dated layout nests under ``folder``.
    """

    category: SpecialCategory | None
    folder: str | None
    keep_dated_layout: bool


def classify_image(
    path: Path,
    *,
    screenshot_config: ScreenshotConfig | None = None,
    photo_graphic_config: PhotoGraphicConfig | None = None,
) -> FileClassification:
    """Run the screenshot and photo-vs-graphic detectors on an image file.

    Args:
        path: Filesystem path to the image.
        screenshot_config: Screenshot detection weights and threshold.
        photo_graphic_config: Photo vs. graphic weights and thresholds.

    Returns:
        A :class:`FileClassification` with both detector results and
        ``live_photo=False``; Live Photo pairing is decided by the caller.

    Raises:
        MediaError: If the file cannot be read as an image.
    """
    return FileClassification(
        screenshot=detect_screenshot(path, config=screenshot_config),
        photo_graphic=detect_photo_or_graphic(path, config=photo_graphic_config),
    )


def resolve_route(
    classification: FileClassification,
    *,
    settings: RoutingSettings,
) -> RouteDecision:
    """Resolve the special-folder route for a classified file.

    Args:
        classification: Classifier outputs for the file.
        settings: Routing folder names and thresholds.

    Returns:
        A :class:`RouteDecision`; ``category`` and ``folder`` are ``None`` when
        the file stays in the plain dated layout.
    """
    category = _resolve_category(classification, settings=settings)
    return RouteDecision(
        category=category,
        folder=None if category is None else _folder_for(category, settings=settings),
        keep_dated_layout=settings.keep_dated_layout,
    )


def plan_destination_dir(
    *,
    decision: RouteDecision,
    dated_dir: PurePath,
) -> PurePath:
    """Compute the destination directory, relative to the organize root.

    Args:
        decision: Route decision for the file.
        dated_dir: Dated layout directory relative to the destination root.

    Returns:
        ``folder/dated_dir`` when routed with the dated layout kept, ``folder``
        when routed without it, and ``dated_dir`` when not routed.
    """
    if decision.folder is None:
        return dated_dir
    folder = PurePath(decision.folder)
    return folder / dated_dir if decision.keep_dated_layout else folder


def _resolve_category(
    classification: FileClassification,
    *,
    settings: RoutingSettings,
) -> SpecialCategory | None:
    """Apply the resolution table and return the first matching category.

    Args:
        classification: Classifier outputs for the file.
        settings: Routing thresholds.

    Returns:
        The matched category, or ``None`` for the dated layout.
    """
    if not settings.enabled:
        return None
    if classification.live_photo:
        return SpecialCategory.LIVE_PHOTO
    threshold = settings.min_confidence
    if _is_confident_screenshot(classification.screenshot, threshold=threshold):
        return SpecialCategory.SCREENSHOT
    if _is_confident_graphic(classification.photo_graphic, threshold=threshold):
        return SpecialCategory.GRAPHIC
    if _needs_review(classification):
        return SpecialCategory.REVIEW
    return None


def _is_confident_screenshot(
    result: ScreenshotClassification | None,
    *,
    threshold: float,
) -> bool:
    """Return whether the screenshot result is positive and confident enough.

    Args:
        result: Screenshot detection result.
        threshold: Minimum confidence required.

    Returns:
        ``True`` when flagged as a screenshot at or above ``threshold``.
    """
    return (
        result is not None and result.is_screenshot and result.confidence >= threshold
    )


def _is_confident_graphic(
    result: PhotoGraphicClassification | None,
    *,
    threshold: float,
) -> bool:
    """Return whether the content result is a confident graphic.

    Args:
        result: Photo vs. graphic classification result.
        threshold: Minimum confidence required.

    Returns:
        ``True`` when classified as a graphic at or above ``threshold``.
    """
    return (
        result is not None
        and result.content_type is ImageContentType.GRAPHIC
        and result.confidence >= threshold
    )


def _needs_review(classification: FileClassification) -> bool:
    """Return whether a low-confidence screenshot or graphic signal fired.

    Args:
        classification: Classifier outputs for the file.

    Returns:
        ``True`` when the file is flagged as a screenshot or graphic at any
        confidence; callers check the confident cases first.
    """
    screenshot = classification.screenshot
    photo_graphic = classification.photo_graphic
    flagged_screenshot = screenshot is not None and screenshot.is_screenshot
    flagged_graphic = (
        photo_graphic is not None
        and photo_graphic.content_type is ImageContentType.GRAPHIC
    )
    return flagged_screenshot or flagged_graphic


def _folder_for(
    category: SpecialCategory,
    *,
    settings: RoutingSettings,
) -> str:
    """Return the configured folder name for a category.

    Args:
        category: The matched special category.
        settings: Routing folder names.

    Returns:
        The folder name configured for ``category``.
    """
    folders = {
        SpecialCategory.SCREENSHOT: settings.screenshots,
        SpecialCategory.GRAPHIC: settings.graphics,
        SpecialCategory.LIVE_PHOTO: settings.live_photos,
        SpecialCategory.REVIEW: settings.review,
    }
    return folders[category]
