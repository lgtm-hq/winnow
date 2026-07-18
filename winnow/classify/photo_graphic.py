"""Photograph vs. graphic classification via EXIF and histogram heuristics.

Photographs and synthetic graphics (screenshots aside) differ in predictable
ways. Photographs usually carry camera EXIF metadata (make, model, exposure) and
have rich color palettes with smooth gradients. Graphics and illustrations tend
to use limited palettes with large flat regions and frequently include an alpha
channel.

Classification combines these signals into competing photo and graphic scores.
Camera EXIF is the primary, decisive signal; the color histogram is the fallback
used when EXIF is absent or inconclusive.

The pure :func:`classify_photo_or_graphic` function operates on already-extracted
signals and needs no third-party dependency. :func:`detect_photo_or_graphic`
reads those signals from a file using Pillow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, auto
from pathlib import Path

from winnow.classify._image import (
    ColorCounts,
    count_colors,
    extract_exif,
    image_has_alpha,
    open_image,
)

DEFAULT_CAMERA_EXIF_TAGS: frozenset[str] = frozenset(
    {
        "Make",
        "Model",
        "LensModel",
        "LensMake",
        "FNumber",
        "ExposureTime",
        "ISOSpeedRatings",
        "PhotographicSensitivity",
        "FocalLength",
        "DateTimeOriginal",
    },
)
"""EXIF tag names whose presence signals a camera-captured photograph."""


class ImageContentType(StrEnum):
    """Content classification for an image."""

    PHOTO = auto()
    GRAPHIC = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class PhotoGraphicConfig:
    """Tunable weights and thresholds for photo vs. graphic classification.

    Attributes:
        camera_weight: Photo confidence added when camera EXIF is present.
        alpha_weight: Graphic confidence added when an alpha channel is present.
        histogram_weight: Confidence added by the histogram fallback to whichever
            side the palette favors.
        graphic_color_threshold: Distinct-color count at or below which a palette
            is considered graphic-like.
        flat_region_ratio: Dominant-color pixel fraction at or above which a
            palette is considered graphic-like (flat regions).
        max_histogram_colors: Upper bound on distinct colors enumerated before a
            palette is treated as photo-like.
        sample_max_dimension: Longest edge to down-sample to before counting
            colors.
        camera_exif_tags: EXIF tag names that indicate a camera capture.
    """

    camera_weight: float = 0.7
    alpha_weight: float = 0.4
    histogram_weight: float = 0.5
    graphic_color_threshold: int = 256
    flat_region_ratio: float = 0.35
    max_histogram_colors: int = 65_536
    sample_max_dimension: int = 256
    camera_exif_tags: frozenset[str] = field(
        default_factory=lambda: DEFAULT_CAMERA_EXIF_TAGS,
    )


@dataclass(frozen=True, slots=True)
class PhotoGraphicSignals:
    """Individual heuristics that contributed to the classification.

    Attributes:
        has_camera_exif: Whether camera EXIF metadata was present.
        has_alpha: Whether the image carries an alpha channel.
        histogram_favors: Which side the color histogram favored, or ``None``
            when no histogram was available.
    """

    has_camera_exif: bool
    has_alpha: bool
    histogram_favors: ImageContentType | None


@dataclass(frozen=True, slots=True)
class PhotoGraphicClassification:
    """Result of photo vs. graphic classification.

    Attributes:
        content_type: The chosen content type.
        confidence: Winner's share of the total score, in ``[0.0, 1.0]``.
        photo_score: Accumulated photo confidence.
        graphic_score: Accumulated graphic confidence.
        signals: The individual signals that contributed to the decision.
    """

    content_type: ImageContentType
    confidence: float
    photo_score: float
    graphic_score: float
    signals: PhotoGraphicSignals


def classify_photo_or_graphic(
    *,
    has_camera_exif: bool = False,
    has_alpha: bool = False,
    color_counts: ColorCounts | None = None,
    config: PhotoGraphicConfig | None = None,
) -> PhotoGraphicClassification:
    """Classify an image as a photograph or graphic from pre-extracted signals.

    Args:
        has_camera_exif: Whether camera EXIF metadata is present.
        has_alpha: Whether the image has an alpha channel.
        color_counts: Optional color histogram summary used as a fallback.
        config: Classification weights and thresholds. Defaults to
            :class:`PhotoGraphicConfig`.

    Returns:
        A :class:`PhotoGraphicClassification` describing the decision.
    """
    active_config = config if config is not None else PhotoGraphicConfig()

    photo_score = 0.0
    graphic_score = 0.0

    if has_camera_exif:
        photo_score += active_config.camera_weight
    if has_alpha:
        graphic_score += active_config.alpha_weight

    histogram_favors = _histogram_favors(
        color_counts=color_counts,
        config=active_config,
    )
    if histogram_favors is ImageContentType.PHOTO:
        photo_score += active_config.histogram_weight
    elif histogram_favors is ImageContentType.GRAPHIC:
        graphic_score += active_config.histogram_weight

    signals = PhotoGraphicSignals(
        has_camera_exif=has_camera_exif,
        has_alpha=has_alpha,
        histogram_favors=histogram_favors,
    )

    total_score = photo_score + graphic_score
    if total_score == 0.0:
        return PhotoGraphicClassification(
            content_type=ImageContentType.UNKNOWN,
            confidence=0.0,
            photo_score=photo_score,
            graphic_score=graphic_score,
            signals=signals,
        )

    if photo_score >= graphic_score:
        content_type = ImageContentType.PHOTO
        confidence = photo_score / total_score
    else:
        content_type = ImageContentType.GRAPHIC
        confidence = graphic_score / total_score

    return PhotoGraphicClassification(
        content_type=content_type,
        confidence=confidence,
        photo_score=photo_score,
        graphic_score=graphic_score,
        signals=signals,
    )


def detect_photo_or_graphic(
    path: Path,
    *,
    config: PhotoGraphicConfig | None = None,
    use_histogram: bool = True,
) -> PhotoGraphicClassification:
    """Classify an image file as a photograph or graphic.

    Reads camera EXIF, alpha, and (optionally) a color histogram from the file
    using Pillow, then applies :func:`classify_photo_or_graphic`.

    Args:
        path: Filesystem path to the image.
        config: Classification weights and thresholds. Defaults to
            :class:`PhotoGraphicConfig`.
        use_histogram: Whether to compute the histogram fallback. Disable to rely
            on EXIF and alpha only.

    Returns:
        A :class:`PhotoGraphicClassification` describing the decision.

    Raises:
        MediaError: If Pillow is unavailable or the file cannot be read.
    """
    active_config = config if config is not None else PhotoGraphicConfig()

    with open_image(path) as image:
        exif = extract_exif(image)
        has_alpha = image_has_alpha(image)
        color_counts = (
            count_colors(
                image,
                max_colors=active_config.max_histogram_colors,
                sample_max_dimension=active_config.sample_max_dimension,
            )
            if use_histogram
            else None
        )

    return classify_photo_or_graphic(
        has_camera_exif=_has_camera_exif(exif=exif, config=active_config),
        has_alpha=has_alpha,
        color_counts=color_counts,
        config=active_config,
    )


def _has_camera_exif(
    *,
    exif: Mapping[str, str],
    config: PhotoGraphicConfig,
) -> bool:
    """Return whether EXIF metadata indicates a camera capture.

    Args:
        exif: Extracted EXIF tag mapping.
        config: Classification config providing the camera tag names.

    Returns:
        ``True`` when any configured camera EXIF tag holds a value.
    """
    return any(exif.get(tag) for tag in config.camera_exif_tags)


def _histogram_favors(
    *,
    color_counts: ColorCounts | None,
    config: PhotoGraphicConfig,
) -> ImageContentType | None:
    """Return which content type the color histogram favors.

    Args:
        color_counts: Color histogram summary, if available.
        config: Classification config providing palette thresholds.

    Returns:
        :attr:`ImageContentType.PHOTO` for rich palettes,
        :attr:`ImageContentType.GRAPHIC` for limited or flat palettes, or
        ``None`` when no histogram was supplied.
    """
    if color_counts is None:
        return None

    is_flat = color_counts.dominant_pixel_ratio >= config.flat_region_ratio
    is_limited_palette = color_counts.distinct_colors <= config.graphic_color_threshold
    if color_counts.exceeds_max:
        return ImageContentType.PHOTO
    if is_limited_palette or is_flat:
        return ImageContentType.GRAPHIC
    return ImageContentType.PHOTO
