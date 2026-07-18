"""Pillow-backed Laplacian-variance sharpness measurement.

Implements the default :class:`~winnow.dedup.quality.SharpnessProvider` used by
the quality comparator. Sharpness is measured as the variance of the Laplacian
of the grayscale image: blurry images have weak second derivatives everywhere,
while sharp images produce strong positive and negative edge responses.

Pillow's :class:`~PIL.ImageFilter.Kernel` clips convolution output of ``"L"``
images to ``[0, 255]``, which would silently discard the negative half of the
Laplacian response and bias the metric toward bright-on-dark edges. To keep both
polarities, the image is convolved twice — once with the Laplacian kernel and
once with its negation, each downscaled so no response can exceed 255 — and the
variance of the signed response is reconstructed from the two clipped halves.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

_LAPLACIAN_KERNEL: tuple[int, ...] = (0, 1, 0, 1, -4, 1, 0, 1, 0)
_NEGATED_LAPLACIAN_KERNEL: tuple[int, ...] = tuple(
    -value for value in _LAPLACIAN_KERNEL
)

# The 3x3 Laplacian response of an 8-bit image lies in [-1020, 1020]; dividing
# by 4 maps each clipped polarity into [0, 255] without saturating.
_KERNEL_SCALE = 4


def laplacian_sharpness(path: Path) -> float | None:
    """Measure image sharpness as the variance of a Laplacian filter.

    Args:
        path: Filesystem path to an image file.

    Returns:
        Variance of the signed Laplacian response of the grayscale image, or
        ``None`` when the file cannot be opened and processed as an image.
    """
    try:
        with Image.open(path) as image:
            grayscale = image.convert("L")
        positive = _filtered(grayscale, kernel=_LAPLACIAN_KERNEL)
        negative = _filtered(grayscale, kernel=_NEGATED_LAPLACIAN_KERNEL)
        return _signed_variance(positive=positive, negative=negative)
    except (OSError, ValueError, IndexError):
        return None


def _filtered(grayscale: Image.Image, *, kernel: tuple[int, ...]) -> Image.Image:
    """Convolve a grayscale image with a downscaled 3x3 kernel.

    Args:
        grayscale: Grayscale (``"L"`` mode) source image.
        kernel: Nine kernel coefficients in row-major order.

    Returns:
        Filtered image holding one clipped polarity of the response, with the
        unfiltered one-pixel border removed.
    """
    kernel_filter = ImageFilter.Kernel(
        size=(3, 3),
        kernel=kernel,
        scale=_KERNEL_SCALE,
    )
    return _crop_unfiltered_border(grayscale.filter(kernel_filter))


def _crop_unfiltered_border(edges: Image.Image) -> Image.Image:
    """Drop the one-pixel border a 3x3 kernel leaves unprocessed.

    Pillow copies the outermost pixels through unchanged, so their original
    intensities would otherwise skew the variance. The border is removed only
    when the image is large enough to leave an interior region.

    Args:
        edges: Laplacian-filtered image.

    Returns:
        The interior of the filtered image, or the original image when it is too
        small to crop.
    """
    width, height = edges.size
    if width <= 2 or height <= 2:
        return edges
    return edges.crop((1, 1, width - 1, height - 1))


def _signed_variance(*, positive: Image.Image, negative: Image.Image) -> float:
    """Reconstruct the variance of a signed response from its clipped halves.

    At every pixel at most one of the two clipped polarities is non-zero, so the
    signed response is ``positive - negative`` and its second moment is the sum
    of the per-half second moments. Each half's mean and variance come from
    :class:`~PIL.ImageStat.Stat`, and the result is rescaled to undo the kernel
    downscaling.

    Args:
        positive: Clipped positive half of the response.
        negative: Clipped negative half of the response.

    Returns:
        Variance of the signed, full-scale Laplacian response.
    """
    stat_positive = ImageStat.Stat(positive)
    stat_negative = ImageStat.Stat(negative)
    mean_positive = stat_positive.mean[0]
    mean_negative = stat_negative.mean[0]
    second_moment = (
        stat_positive.var[0]
        + mean_positive**2
        + stat_negative.var[0]
        + mean_negative**2
    )
    variance = second_moment - (mean_positive - mean_negative) ** 2
    return float(max(variance, 0.0) * _KERNEL_SCALE**2)


__all__ = ["laplacian_sharpness"]
