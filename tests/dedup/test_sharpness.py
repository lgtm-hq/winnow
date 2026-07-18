"""Tests for the Pillow-backed Laplacian sharpness provider."""

from __future__ import annotations

from pathlib import Path

from assertpy import assert_that
from PIL import Image

from winnow.dedup.sharpness import laplacian_sharpness


def _save_grayscale(path: Path, pixels: list[int], *, size: tuple[int, int]) -> None:
    """Write a grayscale PNG built from flat pixel data.

    Args:
        path: Destination PNG path.
        pixels: Row-major 8-bit pixel intensities.
        size: Image ``(width, height)``.
    """
    image = Image.new("L", size)
    image.putdata(pixels)
    image.save(path)


def _checkerboard(*, size: int, inverted: bool = False) -> list[int]:
    """Build checkerboard pixel data.

    Args:
        size: Width and height of the square board.
        inverted: Whether to swap the bright and dark squares.

    Returns:
        Row-major pixel intensities.
    """
    bright, dark = (0, 255) if inverted else (255, 0)
    return [
        bright if (row + column) % 2 == 0 else dark
        for row in range(size)
        for column in range(size)
    ]


def test_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    """A non-image file yields ``None`` rather than raising."""
    not_an_image = tmp_path / "note.txt"
    not_an_image.write_text("not an image", encoding="utf-8")

    assert_that(laplacian_sharpness(not_an_image)).is_none()


def test_returns_none_for_missing_file(tmp_path: Path) -> None:
    """A missing path yields ``None`` rather than raising."""
    assert_that(laplacian_sharpness(tmp_path / "missing.png")).is_none()


def test_detailed_image_measures_sharper_than_flat_one(tmp_path: Path) -> None:
    """A high-frequency image measures sharper than a uniform one."""
    flat = tmp_path / "flat.png"
    _save_grayscale(flat, [128] * (64 * 64), size=(64, 64))
    detailed = tmp_path / "detailed.png"
    _save_grayscale(detailed, _checkerboard(size=64), size=(64, 64))

    flat_sharpness = laplacian_sharpness(flat)
    detailed_sharpness = laplacian_sharpness(detailed)

    assert_that(flat_sharpness).is_equal_to(0.0)
    assert_that(detailed_sharpness).is_not_none()
    assert_that(detailed_sharpness).is_greater_than(flat_sharpness)


def test_sharpness_is_symmetric_across_contrast_polarity(tmp_path: Path) -> None:
    """Bright-on-dark and dark-on-bright edges measure identically.

    Pillow clips ``"L"``-mode convolution output at zero, so a single-polarity
    Laplacian would score inverted copies of the same image differently. The
    two-pass measurement keeps both polarities.
    """
    dot = [0] * (32 * 32)
    dot[16 * 32 + 16] = 255
    inverted_dot = [255 - value for value in dot]
    bright_on_dark = tmp_path / "dot.png"
    dark_on_bright = tmp_path / "inverted_dot.png"
    _save_grayscale(bright_on_dark, dot, size=(32, 32))
    _save_grayscale(dark_on_bright, inverted_dot, size=(32, 32))

    assert_that(laplacian_sharpness(dark_on_bright)).is_close_to(
        laplacian_sharpness(bright_on_dark),
        1e-6,
    )


def test_matches_true_laplacian_variance(tmp_path: Path) -> None:
    """The measured value matches an exact signed-Laplacian variance."""
    size = 16
    pixels = _checkerboard(size=size)
    path = tmp_path / "checker.png"
    _save_grayscale(path, pixels, size=(size, size))
    responses = [
        pixels[(row - 1) * size + column]
        + pixels[row * size + column - 1]
        - 4 * pixels[row * size + column]
        + pixels[row * size + column + 1]
        + pixels[(row + 1) * size + column]
        for row in range(1, size - 1)
        for column in range(1, size - 1)
    ]
    mean = sum(responses) / len(responses)
    expected = sum((value - mean) ** 2 for value in responses) / len(responses)

    assert_that(laplacian_sharpness(path)).is_close_to(expected, 1e-6)


def test_tiny_image_skips_border_crop(tmp_path: Path) -> None:
    """An image too small to crop still yields a finite measurement."""
    path = tmp_path / "tiny.png"
    _save_grayscale(path, [0, 255, 255, 0], size=(2, 2))

    assert_that(laplacian_sharpness(path)).is_not_none()
