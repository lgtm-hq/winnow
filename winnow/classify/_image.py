"""Pillow-backed image feature extraction shared by classifiers.

The classifiers in :mod:`winnow.classify` keep their decision logic in pure,
standard-library functions that operate on already-extracted signals. This module
provides the Pillow bridge that reads those signals (dimensions, EXIF tags, alpha
channel, color histograms, PNG text chunks, and XMP packets) from image files on
disk.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from PIL import ExifTags, Image, UnidentifiedImageError

from winnow.exceptions import MediaError

_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})
_PNG_XMP_TEXT_KEY = "XML:com.adobe.xmp"


@dataclass(frozen=True, slots=True)
class ColorCounts:
    """Summary of an image's color distribution.

    Attributes:
        total_pixels: Number of pixels inspected (after any down-sampling).
        distinct_colors: Count of distinct colors, capped at ``max_colors``.
        dominant_pixel_ratio: Fraction of pixels sharing the single most common
            color, in the range ``[0.0, 1.0]``.
        exceeds_max: Whether the image has more distinct colors than
            ``max_colors`` (indicating a rich, photo-like palette).
    """

    total_pixels: int
    distinct_colors: int
    dominant_pixel_ratio: float
    exceeds_max: bool


@contextmanager
def open_image(path: Path) -> Iterator[Image.Image]:
    """Open an image file for inspection.

    Only the open-and-load phase is translated into a
    :class:`~winnow.exceptions.MediaError`; exceptions raised by caller code
    inside the ``with`` block propagate unchanged.

    Args:
        path: Filesystem path to the image.

    Yields:
        A loaded Pillow image.

    Raises:
        MediaError: If the file cannot be read as an image.
    """
    try:
        image = Image.open(path)
        image.load()
    except (OSError, UnidentifiedImageError) as error:
        raise MediaError(
            f"could not read image: {error}",
            operation="classify",
            file_path=path,
        ) from error
    with image:
        yield image


def extract_dimensions(image: Image.Image) -> tuple[int, int]:
    """Return an image's pixel dimensions.

    Args:
        image: Open Pillow image.

    Returns:
        A ``(width, height)`` tuple.
    """
    return (image.width, image.height)


def image_has_alpha(image: Image.Image) -> bool:
    """Return whether an image carries transparency information.

    Args:
        image: Open Pillow image.

    Returns:
        ``True`` when the image has an alpha channel or transparency metadata.
    """
    if image.mode in _ALPHA_MODES:
        return True
    return "transparency" in image.info


def extract_exif(image: Image.Image) -> Mapping[str, str]:
    """Extract EXIF tags as a mapping of human-readable names to string values.

    Both the base IFD and the EXIF sub-IFD are merged; base-IFD values win on
    name collisions.

    Args:
        image: Open Pillow image.

    Returns:
        Mapping of EXIF tag names to normalized string values. Empty when the
        image has no EXIF metadata.
    """
    raw_exif = image.getexif()
    if not raw_exif:
        return {}

    tags: dict[str, str] = {}
    for tag_id, value in raw_exif.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        stringified = _stringify_exif_value(value)
        if stringified:
            tags[name] = stringified

    sub_ifd = raw_exif.get_ifd(ExifTags.IFD.Exif)
    for tag_id, value in sub_ifd.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        stringified = _stringify_exif_value(value)
        if stringified:
            tags.setdefault(name, stringified)
    return tags


def extract_text_chunks(image: Image.Image) -> Mapping[str, str]:
    """Return an image's textual metadata chunks.

    Only PNG exposes these (``tEXt``/``zTXt``/``iTXt`` chunks via the ``text``
    attribute); other formats yield an empty mapping.

    Args:
        image: Open Pillow image.

    Returns:
        Mapping of chunk keyword to decoded text; empty when unavailable.
    """
    text = getattr(image, "text", None)
    if not isinstance(text, Mapping):
        return {}
    return {str(key): str(value) for key, value in text.items()}


def extract_xmp(image: Image.Image) -> str | None:
    """Return an image's raw XMP packet as text, if present.

    Reads ``image.info["xmp"]`` (JPEG, TIFF, WebP, and PNG when Pillow surfaces
    it) or the PNG ``XML:com.adobe.xmp`` text chunk. The packet is returned as
    is; no XML parsing is performed.

    Args:
        image: Open Pillow image.

    Returns:
        The XMP text, or ``None`` when the image has no XMP metadata.
    """
    raw = image.info.get("xmp")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    return extract_text_chunks(image).get(_PNG_XMP_TEXT_KEY)


def count_colors(
    image: Image.Image,
    *,
    max_colors: int,
    sample_max_dimension: int,
) -> ColorCounts:
    """Summarize the color distribution of an image.

    Large images are down-sampled with nearest-neighbor resampling so flat
    regions are preserved while keeping the count bounded.

    Args:
        image: Open Pillow image.
        max_colors: Upper bound on distinct colors to enumerate before treating
            the palette as photo-like.
        sample_max_dimension: Longest edge to down-sample to before counting.

    Returns:
        A :class:`ColorCounts` summary of the sampled image.
    """
    sample = _prepare_color_sample(
        image=image,
        sample_max_dimension=sample_max_dimension,
    )
    try:
        total_pixels = sample.width * sample.height
        colors = sample.getcolors(maxcolors=max_colors)
    finally:
        if sample is not image:
            sample.close()
    if colors is None:
        return ColorCounts(
            total_pixels=total_pixels,
            distinct_colors=max_colors,
            dominant_pixel_ratio=0.0,
            exceeds_max=True,
        )

    dominant_count = max((count for count, _ in colors), default=0)
    dominant_ratio = dominant_count / total_pixels if total_pixels else 0.0
    return ColorCounts(
        total_pixels=total_pixels,
        distinct_colors=len(colors),
        dominant_pixel_ratio=dominant_ratio,
        exceeds_max=False,
    )


def _prepare_color_sample(
    *,
    image: Image.Image,
    sample_max_dimension: int,
) -> Image.Image:
    """Return an RGB view of an image, down-sampled if oversized.

    The original image may be returned unchanged (when it is already a small
    RGB image); any intermediate converted image is closed before returning.

    Args:
        image: Open Pillow image.
        sample_max_dimension: Longest edge to down-sample to.

    Returns:
        An RGB image no larger than ``sample_max_dimension`` on its longest edge.
        Callers own (and should close) the result only when it is not the
        original image.
    """
    rgb_image = image if image.mode == "RGB" else image.convert("RGB")
    longest_edge = max(rgb_image.width, rgb_image.height)
    if longest_edge <= sample_max_dimension:
        return rgb_image

    scale = sample_max_dimension / longest_edge
    target_size = (
        max(1, round(rgb_image.width * scale)),
        max(1, round(rgb_image.height * scale)),
    )
    try:
        return rgb_image.resize(target_size, resample=Image.Resampling.NEAREST)
    finally:
        if rgb_image is not image:
            rgb_image.close()


def _stringify_exif_value(value: object) -> str:
    """Normalize a raw EXIF value into a trimmed string.

    Args:
        value: Raw EXIF value (``str``, ``bytes``, or other).

    Returns:
        A whitespace- and null-trimmed string representation.
    """
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="replace")
        return decoded.replace("\x00", "").strip()
    return str(value).replace("\x00", "").strip()
