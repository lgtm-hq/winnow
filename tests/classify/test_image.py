"""Tests for the Pillow-backed image feature extraction helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from tests.classify.conftest import ImageFactory
from winnow.classify import ColorCounts
from winnow.classify import _image as image_module
from winnow.exceptions import MediaError


def test_open_image_raises_media_error_on_non_image(tmp_path: Path) -> None:
    """Opening a non-image file raises a MediaError, not a Pillow error."""
    bogus = tmp_path / "notes.txt"
    bogus.write_text("not an image", encoding="utf-8")

    with pytest.raises(MediaError, match="could not read image"):
        with image_module.open_image(bogus):
            pass


def test_count_colors_reports_flat_image(make_image: ImageFactory) -> None:
    """A solid image reports a single dominant color."""
    path = make_image(name="flat.png", size=(20, 20), colors=[(5, 5, 5)])

    with image_module.open_image(path) as image:
        counts = image_module.count_colors(
            image,
            max_colors=4096,
            sample_max_dimension=256,
        )

    assert_that(counts).is_instance_of(ColorCounts)
    assert_that(counts.distinct_colors).is_equal_to(1)
    assert_that(counts.dominant_pixel_ratio).is_equal_to(1.0)
    assert_that(counts.exceeds_max).is_false()


def test_count_colors_flags_exceeding_max(
    make_image: ImageFactory,
    gradient_colors: list[tuple[int, int, int]],
) -> None:
    """A palette larger than the cap sets the exceeds-max flag."""
    path = make_image(name="rich.png", size=(64, 64), colors=gradient_colors)

    with image_module.open_image(path) as image:
        counts = image_module.count_colors(
            image,
            max_colors=8,
            sample_max_dimension=256,
        )

    assert_that(counts.exceeds_max).is_true()
    assert_that(counts.distinct_colors).is_equal_to(8)


def test_count_colors_downsamples_large_images(make_image: ImageFactory) -> None:
    """Oversized images are down-sampled before counting."""
    path = make_image(name="big.png", size=(600, 400), colors=[(1, 2, 3)])

    with image_module.open_image(path) as image:
        counts = image_module.count_colors(
            image,
            max_colors=4096,
            sample_max_dimension=64,
        )

    assert_that(counts.total_pixels).is_less_than_or_equal_to(64 * 64)


def test_image_has_alpha_detects_transparency_info(make_image: ImageFactory) -> None:
    """Palette images with transparency info are reported as having alpha."""
    path = make_image(name="badge.png", size=(8, 8), mode="RGBA", colors=[(0, 0, 0, 0)])

    with image_module.open_image(path) as image:
        assert_that(image_module.image_has_alpha(image)).is_true()


def test_extract_exif_returns_empty_without_metadata(
    make_image: ImageFactory,
) -> None:
    """Images without EXIF yield an empty mapping."""
    path = make_image(name="plain.png", size=(8, 8))

    with image_module.open_image(path) as image:
        exif = image_module.extract_exif(image)

    assert_that(dict(exif)).is_empty()


def test_extract_exif_merges_sub_ifd_tags(make_image: ImageFactory) -> None:
    """EXIF sub-IFD tags (for example FNumber) are merged into the mapping."""
    _exif_fnumber_tag = 0x829D
    path = make_image(
        name="camera.jpg",
        size=(16, 16),
        exif_ifd={_exif_fnumber_tag: 4.0},
    )

    with image_module.open_image(path) as image:
        exif = image_module.extract_exif(image)

    assert_that(dict(exif)).contains_key("FNumber")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"gnome-screenshot\x00", "gnome-screenshot"),
        ("  Canon  ", "Canon"),
        (b"", ""),
    ],
)
def test_stringify_exif_value_normalizes_bytes_and_text(
    value: bytes | str,
    expected: str,
) -> None:
    """Raw EXIF values are decoded and trimmed of nulls and whitespace."""
    assert_that(image_module._stringify_exif_value(value)).is_equal_to(expected)
