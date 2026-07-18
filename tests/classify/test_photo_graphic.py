"""Tests for photo vs. graphic classification heuristics."""

from __future__ import annotations

from assertpy import assert_that

from tests.classify.conftest import ImageFactory
from winnow.classify import (
    ColorCounts,
    ImageContentType,
    PhotoGraphicConfig,
    classify_photo_or_graphic,
    detect_photo_or_graphic,
)

_EXIF_MAKE_TAG = 0x010F
_EXIF_MODEL_TAG = 0x0110


def _photo_histogram() -> ColorCounts:
    """Return a histogram summary resembling a photograph.

    Returns:
        A rich-palette, low-flatness color summary.
    """
    return ColorCounts(
        total_pixels=4096,
        distinct_colors=3000,
        dominant_pixel_ratio=0.05,
        exceeds_max=False,
    )


def _graphic_histogram() -> ColorCounts:
    """Return a histogram summary resembling a flat graphic.

    Returns:
        A limited-palette, high-flatness color summary.
    """
    return ColorCounts(
        total_pixels=4096,
        distinct_colors=4,
        dominant_pixel_ratio=0.8,
        exceeds_max=False,
    )


def test_camera_exif_classifies_as_photo() -> None:
    """Camera EXIF is the decisive primary photo signal."""
    result = classify_photo_or_graphic(has_camera_exif=True)

    assert_that(result.content_type).is_equal_to(ImageContentType.PHOTO)
    assert_that(result.confidence).is_equal_to(1.0)


def test_camera_exif_overrides_alpha_channel() -> None:
    """A photo saved with an alpha channel is still classified as a photo."""
    result = classify_photo_or_graphic(has_camera_exif=True, has_alpha=True)

    assert_that(result.content_type).is_equal_to(ImageContentType.PHOTO)
    assert_that(result.photo_score).is_greater_than(result.graphic_score)


def test_alpha_channel_leans_graphic() -> None:
    """An alpha channel without other signals leans graphic."""
    result = classify_photo_or_graphic(has_alpha=True)

    assert_that(result.content_type).is_equal_to(ImageContentType.GRAPHIC)


def test_histogram_fallback_detects_photo() -> None:
    """A rich palette classifies as a photo when EXIF is absent."""
    result = classify_photo_or_graphic(color_counts=_photo_histogram())

    assert_that(result.content_type).is_equal_to(ImageContentType.PHOTO)
    assert_that(result.signals.histogram_favors).is_equal_to(ImageContentType.PHOTO)


def test_histogram_fallback_detects_graphic() -> None:
    """A limited, flat palette classifies as a graphic when EXIF is absent."""
    result = classify_photo_or_graphic(color_counts=_graphic_histogram())

    assert_that(result.content_type).is_equal_to(ImageContentType.GRAPHIC)
    assert_that(result.signals.histogram_favors).is_equal_to(ImageContentType.GRAPHIC)


def test_exceeds_max_colors_is_photo() -> None:
    """Palettes richer than the enumeration cap are treated as photos."""
    rich = ColorCounts(
        total_pixels=10_000,
        distinct_colors=65_536,
        dominant_pixel_ratio=0.0,
        exceeds_max=True,
    )

    result = classify_photo_or_graphic(color_counts=rich)

    assert_that(result.content_type).is_equal_to(ImageContentType.PHOTO)


def test_flat_region_forces_graphic_despite_many_colors() -> None:
    """A dominant flat region classifies as graphic even with many colors."""
    flat = ColorCounts(
        total_pixels=4096,
        distinct_colors=1000,
        dominant_pixel_ratio=0.6,
        exceeds_max=False,
    )

    result = classify_photo_or_graphic(color_counts=flat)

    assert_that(result.content_type).is_equal_to(ImageContentType.GRAPHIC)


def test_no_signals_yield_unknown() -> None:
    """With no signals the classifier reports UNKNOWN."""
    result = classify_photo_or_graphic()

    assert_that(result.content_type).is_equal_to(ImageContentType.UNKNOWN)
    assert_that(result.confidence).is_equal_to(0.0)
    assert_that(result.signals.histogram_favors).is_none()


def test_competing_signals_report_relative_confidence() -> None:
    """Confidence reflects the winner's share of the total score."""
    result = classify_photo_or_graphic(has_camera_exif=True, has_alpha=True)

    expected = 0.7 / (0.7 + 0.4)
    assert_that(result.confidence).is_close_to(expected, tolerance=0.001)


def test_custom_thresholds_are_configurable() -> None:
    """A stricter color threshold reclassifies a mid-palette image."""
    counts = ColorCounts(
        total_pixels=4096,
        distinct_colors=200,
        dominant_pixel_ratio=0.05,
        exceeds_max=False,
    )
    strict = PhotoGraphicConfig(graphic_color_threshold=64)

    result = classify_photo_or_graphic(color_counts=counts, config=strict)

    assert_that(result.content_type).is_equal_to(ImageContentType.PHOTO)


def test_detect_photo_from_camera_exif(make_image: ImageFactory) -> None:
    """File-based detection reads camera make/model from EXIF."""
    path = make_image(
        name="dsc.jpg",
        size=(32, 32),
        exif={_EXIF_MAKE_TAG: "Canon", _EXIF_MODEL_TAG: "EOS R5"},
    )

    result = detect_photo_or_graphic(path)

    assert_that(result.signals.has_camera_exif).is_true()
    assert_that(result.content_type).is_equal_to(ImageContentType.PHOTO)


def test_detect_graphic_from_flat_png(make_image: ImageFactory) -> None:
    """A flat solid-color PNG is detected as a graphic."""
    path = make_image(name="flat.png", size=(32, 32), colors=[(10, 20, 30)])

    result = detect_photo_or_graphic(path)

    assert_that(result.content_type).is_equal_to(ImageContentType.GRAPHIC)


def test_detect_graphic_from_alpha_channel(make_image: ImageFactory) -> None:
    """An RGBA image is detected as a graphic via its alpha channel."""
    path = make_image(
        name="badge.png",
        size=(16, 16),
        mode="RGBA",
        colors=[(200, 50, 50, 128)],
    )

    result = detect_photo_or_graphic(path)

    assert_that(result.signals.has_alpha).is_true()
    assert_that(result.content_type).is_equal_to(ImageContentType.GRAPHIC)


def test_detect_photo_from_rich_histogram(
    make_image: ImageFactory,
    gradient_colors: list[tuple[int, int, int]],
) -> None:
    """A many-colored image without EXIF is detected as a photo via histogram."""
    path = make_image(name="gradient.png", size=(64, 64), colors=gradient_colors)

    result = detect_photo_or_graphic(path)

    assert_that(result.signals.has_camera_exif).is_false()
    assert_that(result.content_type).is_equal_to(ImageContentType.PHOTO)


def test_detect_can_skip_histogram(make_image: ImageFactory) -> None:
    """Disabling the histogram relies on EXIF and alpha only."""
    path = make_image(name="flat.png", size=(32, 32), colors=[(10, 20, 30)])

    result = detect_photo_or_graphic(path, use_histogram=False)

    assert_that(result.signals.histogram_favors).is_none()
    assert_that(result.content_type).is_equal_to(ImageContentType.UNKNOWN)
