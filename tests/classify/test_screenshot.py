"""Tests for screenshot detection heuristics."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from tests.classify.conftest import ImageFactory
from winnow.classify import (
    ScreenshotConfig,
    classify_screenshot,
    detect_screenshot,
)

_EXIF_SOFTWARE_TAG = 0x0131


@pytest.mark.parametrize(
    "filename",
    [
        "Screenshot_20240101-120000.png",
        "Screen Shot 2024-01-01 at 12.00.00.png",
        "screen-shot.png",
        "screencapture-example.png",
        "my_screencap.jpg",
        "Capture-d-ecran.png",
    ],
)
def test_classify_flags_known_screenshot_filenames(filename: str) -> None:
    """Recognized screenshot filename patterns cross the default threshold."""
    result = classify_screenshot(filename=filename)

    assert_that(result.is_screenshot).is_true()
    assert_that(result.signals.filename_match).is_true()


@pytest.mark.parametrize(
    "filename",
    [
        "IMG_1234.jpg",
        "vacation-beach.png",
        "document-scan.tiff",
    ],
)
def test_classify_ignores_ordinary_filenames(filename: str) -> None:
    """Ordinary filenames do not match screenshot patterns."""
    result = classify_screenshot(filename=filename)

    assert_that(result.signals.filename_match).is_false()
    assert_that(result.is_screenshot).is_false()


@pytest.mark.parametrize(
    "software",
    [
        "gnome-screenshot 42.0",
        "Screenshot",
        "Flameshot",
        "Snipping Tool",
        "Spectacle",
    ],
)
def test_classify_flags_capture_software(software: str) -> None:
    """EXIF software naming a capture tool is a strong screenshot signal."""
    result = classify_screenshot(software=software)

    assert_that(result.signals.software_match).is_true()
    assert_that(result.is_screenshot).is_true()


def test_classify_ignores_camera_software() -> None:
    """Camera firmware software strings are not treated as capture tools."""
    result = classify_screenshot(software="Adobe Photoshop 25.0")

    assert_that(result.signals.software_match).is_false()
    assert_that(result.is_screenshot).is_false()


def test_dimension_alone_is_below_default_threshold() -> None:
    """A common resolution alone is too weak to flag a screenshot by default."""
    result = classify_screenshot(dimensions=(1920, 1080))

    assert_that(result.signals.dimension_match).is_true()
    assert_that(result.is_screenshot).is_false()


def test_dimension_matches_portrait_orientation() -> None:
    """Screen resolutions match regardless of orientation."""
    result = classify_screenshot(dimensions=(1080, 1920))

    assert_that(result.signals.dimension_match).is_true()


def test_filename_and_dimension_combine_to_flag_screenshot() -> None:
    """Weaker signals accumulate to cross the threshold together."""
    result = classify_screenshot(
        filename="Screenshot.png",
        dimensions=(2560, 1440),
    )

    assert_that(result.confidence).is_close_to(0.9, tolerance=0.001)
    assert_that(result.is_screenshot).is_true()


def test_confidence_is_clamped_to_one() -> None:
    """Combined signals never exceed a confidence of 1.0."""
    result = classify_screenshot(
        filename="Screenshot.png",
        software="gnome-screenshot",
        dimensions=(1920, 1080),
    )

    assert_that(result.confidence).is_equal_to(1.0)


def test_lower_threshold_increases_sensitivity() -> None:
    """Lowering the threshold lets a lone dimension match flag a screenshot."""
    sensitive = ScreenshotConfig(threshold=0.3)

    result = classify_screenshot(dimensions=(1366, 768), config=sensitive)

    assert_that(result.is_screenshot).is_true()


def test_extra_resolutions_are_configurable() -> None:
    """Custom resolutions register as screen sizes."""
    config = ScreenshotConfig(extra_resolutions=frozenset({(800, 600)}))

    result = classify_screenshot(dimensions=(600, 800), config=config)

    assert_that(result.signals.dimension_match).is_true()


def test_empty_inputs_yield_no_signals() -> None:
    """Absent signals produce a non-screenshot with zero confidence."""
    result = classify_screenshot()

    assert_that(result.confidence).is_equal_to(0.0)
    assert_that(result.is_screenshot).is_false()


def test_detect_screenshot_reads_exif_software(make_image: ImageFactory) -> None:
    """File-based detection reads capture-tool software from EXIF."""
    path = make_image(
        name="capture.jpg",
        size=(24, 24),
        exif={_EXIF_SOFTWARE_TAG: "gnome-screenshot 42.0"},
    )

    result = detect_screenshot(path)

    assert_that(result.signals.software_match).is_true()
    assert_that(result.is_screenshot).is_true()


def test_detect_screenshot_uses_filename_and_dimensions(
    make_image: ImageFactory,
) -> None:
    """File-based detection combines filename and dimension signals."""
    path = make_image(name="Screenshot_home.png", size=(1280, 720))

    result = detect_screenshot(path)

    assert_that(result.signals.filename_match).is_true()
    assert_that(result.signals.dimension_match).is_true()
    assert_that(result.is_screenshot).is_true()


def test_detect_screenshot_on_ordinary_image(make_image: ImageFactory) -> None:
    """An ordinary small image is not detected as a screenshot."""
    path = make_image(name="photo.jpg", size=(40, 30))

    result = detect_screenshot(path)

    assert_that(result.is_screenshot).is_false()
