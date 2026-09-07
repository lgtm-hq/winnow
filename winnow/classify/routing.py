"""Special-folder route resolution for classified media.

Maps the existing classifier results (screenshot, photograph vs. graphic,
AI-generated) plus the Live Photo signal to a
:class:`~winnow.models.enums.SpecialCategory` and the folder name configured in
:class:`~winnow.models.config.RoutingSettings`.

Resolution order (the first match wins):

1. Routing disabled -> no route.
2. Live Photo -> ``LIVE_PHOTO``.
3. AI-generated at or above ``min_confidence`` -> ``AI_GENERATED``.
4. Screenshot at or above ``min_confidence`` -> ``SCREENSHOT``.
5. Graphic at or above ``min_confidence`` -> ``GRAPHIC``.
6. AI-generated, screenshot, or graphic below ``min_confidence`` -> ``REVIEW``.
7. Otherwise -> no route (dated layout).

``PHOTO`` and ``UNKNOWN`` content never route. ``Duplicates/`` is produced by
the dedup step after execution and is never a route.

:func:`classify_image` is the only function here that touches disk;
:func:`resolve_route` and :func:`plan_destination_dir` are pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath

from winnow.classify.ai_generated import (
    AiGeneratedClassification,
    AiGeneratedConfig,
    detect_ai_generated,
)
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
        ai_generated: AI-generated detection result, or ``None`` when not run.
    """

    screenshot: ScreenshotClassification | None = None
    photo_graphic: PhotoGraphicClassification | None = None
    live_photo: bool = False
    ai_generated: AiGeneratedClassification | None = None


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
    ai_generated_config: AiGeneratedConfig | None = None,
) -> FileClassification:
    """Run the screenshot, photo-vs-graphic, and AI-generated detectors.

    Args:
        path: Filesystem path to the image.
        screenshot_config: Screenshot detection weights and threshold.
        photo_graphic_config: Photo vs. graphic weights and thresholds.
        ai_generated_config: AI-generated detection weights and threshold.

    Returns:
        A :class:`FileClassification` with all detector results and
        ``live_photo=False``; Live Photo pairing is decided by the caller.

    Raises:
        MediaError: If the file cannot be read as an image.
    """
    return FileClassification(
        screenshot=detect_screenshot(path, config=screenshot_config),
        photo_graphic=detect_photo_or_graphic(path, config=photo_graphic_config),
        ai_generated=detect_ai_generated(path, config=ai_generated_config),
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
    threshold = settings.min_confidence
    table = (
        (classification.live_photo, SpecialCategory.LIVE_PHOTO),
        (
            _is_confident_ai_generated(
                classification.ai_generated,
                threshold=threshold,
            ),
            SpecialCategory.AI_GENERATED,
        ),
        (
            _is_confident_screenshot(classification.screenshot, threshold=threshold),
            SpecialCategory.SCREENSHOT,
        ),
        (
            _is_confident_graphic(classification.photo_graphic, threshold=threshold),
            SpecialCategory.GRAPHIC,
        ),
        (_needs_review(classification), SpecialCategory.REVIEW),
    )
    return next((category for matched, category in table if matched), None)


def _is_confident_ai_generated(
    result: AiGeneratedClassification | None,
    *,
    threshold: float,
) -> bool:
    """Return whether the AI-generated result is positive and confident enough.

    Args:
        result: AI-generated detection result.
        threshold: Minimum confidence required.

    Returns:
        ``True`` when flagged as AI-generated at or above ``threshold``.
    """
    return (
        result is not None and result.is_ai_generated and result.confidence >= threshold
    )


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
    """Return whether a low-confidence AI, screenshot, or graphic signal fired.

    Args:
        classification: Classifier outputs for the file.

    Returns:
        ``True`` when the file is flagged as AI-generated, a screenshot, or a
        graphic at any confidence; callers check the confident cases first.
    """
    ai_generated = classification.ai_generated
    screenshot = classification.screenshot
    photo_graphic = classification.photo_graphic
    flagged_ai = ai_generated is not None and ai_generated.is_ai_generated
    flagged_screenshot = screenshot is not None and screenshot.is_screenshot
    flagged_graphic = (
        photo_graphic is not None
        and photo_graphic.content_type is ImageContentType.GRAPHIC
    )
    return flagged_ai or flagged_screenshot or flagged_graphic


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
        SpecialCategory.AI_GENERATED: settings.ai_generated,
        SpecialCategory.REVIEW: settings.review,
    }
    return folders[category]
