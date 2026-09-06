"""Tests for special-folder route resolution."""

from __future__ import annotations

from pathlib import PurePath
from typing import TypeVar

import pytest
from assertpy import assert_that

from tests.classify.conftest import ImageFactory
from winnow.classify.ai_generated import (
    AiGeneratedClassification,
    AiGeneratedSignals,
)
from winnow.classify.photo_graphic import (
    ImageContentType,
    PhotoGraphicClassification,
    PhotoGraphicSignals,
)
from winnow.classify.routing import (
    FileClassification,
    RouteDecision,
    classify_image,
    plan_destination_dir,
    resolve_route,
)
from winnow.classify.screenshot import ScreenshotClassification, ScreenshotSignals
from winnow.models.config import RoutingSettings
from winnow.models.enums import SpecialCategory

T = TypeVar("T")


def _present(value: T | None) -> T:
    """Narrow an optional classifier result, failing the test when missing.

    Args:
        value: Classifier result that must be present.

    Returns:
        The non-``None`` value.
    """
    if value is None:
        pytest.fail("expected a classifier result, got None")
    return value


def _screenshot(*, is_screenshot: bool, confidence: float) -> ScreenshotClassification:
    """Build a screenshot result with neutral signals.

    Args:
        is_screenshot: Whether the detector flagged the image.
        confidence: Summed confidence to report.

    Returns:
        A synthetic :class:`ScreenshotClassification`.
    """
    return ScreenshotClassification(
        is_screenshot=is_screenshot,
        confidence=confidence,
        signals=ScreenshotSignals(
            filename_match=False,
            software_match=False,
            dimension_match=False,
        ),
    )


def _ai(*, is_ai_generated: bool, confidence: float) -> AiGeneratedClassification:
    """Build an AI-generated result with neutral signals.

    Args:
        is_ai_generated: Whether the detector flagged the image.
        confidence: Summed confidence to report.

    Returns:
        A synthetic :class:`AiGeneratedClassification`.
    """
    return AiGeneratedClassification(
        is_ai_generated=is_ai_generated,
        confidence=confidence,
        signals=AiGeneratedSignals(
            c2pa_ai_source=False,
            prompt_chunk=None,
            generator_marker=None,
        ),
    )


def _content(
    *,
    content_type: ImageContentType,
    confidence: float,
) -> PhotoGraphicClassification:
    """Build a photo-vs-graphic result with neutral signals.

    Args:
        content_type: The content type to report.
        confidence: Winner share to report.

    Returns:
        A synthetic :class:`PhotoGraphicClassification`.
    """
    return PhotoGraphicClassification(
        content_type=content_type,
        confidence=confidence,
        photo_score=0.0,
        graphic_score=0.0,
        signals=PhotoGraphicSignals(
            has_camera_exif=False,
            has_alpha=False,
            histogram_favors=None,
        ),
    )


@pytest.mark.parametrize(
    ("classification", "settings", "expected_category", "expected_folder"),
    [
        (
            FileClassification(
                screenshot=_screenshot(is_screenshot=True, confidence=1.0),
                live_photo=True,
            ),
            RoutingSettings(enabled=False),
            None,
            None,
        ),
        (
            FileClassification(
                screenshot=_screenshot(is_screenshot=True, confidence=1.0),
                live_photo=True,
            ),
            RoutingSettings(),
            SpecialCategory.LIVE_PHOTO,
            "LivePhotos",
        ),
        (
            FileClassification(
                ai_generated=_ai(is_ai_generated=True, confidence=0.9),
            ),
            RoutingSettings(),
            SpecialCategory.AI_GENERATED,
            "AI-Generated",
        ),
        (
            FileClassification(
                ai_generated=_ai(is_ai_generated=True, confidence=0.6),
            ),
            RoutingSettings(),
            SpecialCategory.REVIEW,
            "Review",
        ),
        (
            FileClassification(
                screenshot=_screenshot(is_screenshot=True, confidence=1.0),
                ai_generated=_ai(is_ai_generated=True, confidence=0.8),
            ),
            RoutingSettings(),
            SpecialCategory.AI_GENERATED,
            "AI-Generated",
        ),
        (
            FileClassification(
                screenshot=_screenshot(is_screenshot=True, confidence=0.9),
                photo_graphic=_content(
                    content_type=ImageContentType.GRAPHIC,
                    confidence=1.0,
                ),
            ),
            RoutingSettings(),
            SpecialCategory.SCREENSHOT,
            "Screenshots",
        ),
        (
            FileClassification(
                screenshot=_screenshot(is_screenshot=False, confidence=0.3),
                photo_graphic=_content(
                    content_type=ImageContentType.GRAPHIC,
                    confidence=0.8,
                ),
            ),
            RoutingSettings(),
            SpecialCategory.GRAPHIC,
            "Graphics",
        ),
        (
            FileClassification(
                screenshot=_screenshot(is_screenshot=True, confidence=0.6),
            ),
            RoutingSettings(),
            SpecialCategory.REVIEW,
            "Review",
        ),
        (
            FileClassification(
                photo_graphic=_content(
                    content_type=ImageContentType.GRAPHIC,
                    confidence=0.5,
                ),
            ),
            RoutingSettings(),
            SpecialCategory.REVIEW,
            "Review",
        ),
        (
            FileClassification(
                screenshot=_screenshot(is_screenshot=False, confidence=0.0),
                photo_graphic=_content(
                    content_type=ImageContentType.PHOTO,
                    confidence=1.0,
                ),
            ),
            RoutingSettings(),
            None,
            None,
        ),
        (
            FileClassification(
                photo_graphic=_content(
                    content_type=ImageContentType.UNKNOWN,
                    confidence=0.0,
                ),
            ),
            RoutingSettings(),
            None,
            None,
        ),
        (
            FileClassification(),
            RoutingSettings(),
            None,
            None,
        ),
    ],
    ids=[
        "disabled",
        "live_photo",
        "confident_ai_generated",
        "low_confidence_ai_generated_review",
        "ai_generated_outranks_screenshot",
        "confident_screenshot",
        "confident_graphic",
        "low_confidence_screenshot_review",
        "low_confidence_graphic_review",
        "photo_dated",
        "unknown_dated",
        "no_classifiers_dated",
    ],
)
def test_resolve_route_follows_resolution_table(
    classification: FileClassification,
    settings: RoutingSettings,
    expected_category: SpecialCategory | None,
    expected_folder: str | None,
) -> None:
    """Verify each row of the resolution table yields the documented category."""
    decision = resolve_route(classification, settings=settings)

    assert_that(decision.category).is_equal_to(expected_category)
    assert_that(decision.folder).is_equal_to(expected_folder)
    assert_that(decision.keep_dated_layout).is_equal_to(settings.keep_dated_layout)


def test_resolve_route_uses_renamed_folder() -> None:
    """Verify a renamed category folder appears in the decision."""
    classification = FileClassification(
        photo_graphic=_content(content_type=ImageContentType.GRAPHIC, confidence=1.0),
    )

    decision = resolve_route(
        classification,
        settings=RoutingSettings(graphics="Memes"),
    )

    assert_that(decision.category).is_equal_to(SpecialCategory.GRAPHIC)
    assert_that(decision.folder).is_equal_to("Memes")


def test_resolve_route_honours_min_confidence() -> None:
    """Verify a custom min_confidence moves a result between confident and review."""
    classification = FileClassification(
        screenshot=_screenshot(is_screenshot=True, confidence=0.6),
    )

    strict = resolve_route(classification, settings=RoutingSettings())
    lenient = resolve_route(
        classification,
        settings=RoutingSettings(min_confidence=0.5),
    )

    assert_that(strict.category).is_equal_to(SpecialCategory.REVIEW)
    assert_that(lenient.category).is_equal_to(SpecialCategory.SCREENSHOT)


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (
            RouteDecision(
                category=SpecialCategory.SCREENSHOT,
                folder="Screenshots",
                keep_dated_layout=True,
            ),
            PurePath("Screenshots", "2024", "03-March"),
        ),
        (
            RouteDecision(
                category=SpecialCategory.SCREENSHOT,
                folder="Screenshots",
                keep_dated_layout=False,
            ),
            PurePath("Screenshots"),
        ),
        (
            RouteDecision(category=None, folder=None, keep_dated_layout=True),
            PurePath("2024", "03-March"),
        ),
        (
            RouteDecision(category=None, folder=None, keep_dated_layout=False),
            PurePath("2024", "03-March"),
        ),
    ],
    ids=[
        "routed_keep_dated",
        "routed_flat",
        "unrouted_keep_dated",
        "unrouted_flat",
    ],
)
def test_plan_destination_dir(decision: RouteDecision, expected: PurePath) -> None:
    """Verify the destination prefix follows the route and layout flags."""
    dated_dir = PurePath("2024", "03-March")

    result = plan_destination_dir(decision=decision, dated_dir=dated_dir)

    assert_that(result).is_equal_to(expected)


def test_classify_image_flags_screenshot_named_png(make_image: ImageFactory) -> None:
    """Verify a screenshot-named, screen-sized PNG is detected as a screenshot."""
    path = make_image(name="Screenshot_2024.png", size=(1920, 1080))

    classification = classify_image(path)

    assert_that(classification.photo_graphic).is_instance_of(
        PhotoGraphicClassification,
    )
    assert_that(_present(classification.screenshot).is_screenshot).is_true()
    assert_that(_present(classification.ai_generated).is_ai_generated).is_false()
    assert_that(classification.live_photo).is_false()


def test_classify_image_flags_camera_jpeg_as_photo(make_image: ImageFactory) -> None:
    """Verify a camera-EXIF JPEG is classified as a photograph, not a screenshot."""
    path = make_image(name="IMG_1.jpg", exif={0x010F: "Canon", 0x0110: "EOS"})

    classification = classify_image(path)

    assert_that(_present(classification.screenshot).is_screenshot).is_false()
    assert_that(_present(classification.photo_graphic).content_type).is_equal_to(
        ImageContentType.PHOTO,
    )
    assert_that(classification.live_photo).is_false()


def test_classify_image_feeds_resolve_route(make_image: ImageFactory) -> None:
    """Verify the disk classifier output routes a screenshot end to end."""
    path = make_image(name="Screenshot_2024.png", size=(1920, 1080))

    decision = resolve_route(classify_image(path), settings=RoutingSettings())

    assert_that(decision.category).is_equal_to(SpecialCategory.SCREENSHOT)
    assert_that(decision.folder).is_equal_to("Screenshots")
