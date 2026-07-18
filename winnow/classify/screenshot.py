"""Screenshot detection via EXIF, filename, and dimension heuristics.

Screenshots rarely carry camera EXIF metadata but frequently expose other tells:
a screen-capture tool recorded in the EXIF ``Software`` tag, a recognizable
filename pattern (for example ``Screenshot_20240101-120000.png`` or
``Screen Shot 2024-01-01 at 12.00.00.png``), and pixel dimensions that look like
a display panel: screen-sized, with an aspect ratio matching a common desktop,
phone, or ultrawide panel.

Detection is a weighted score. Each fired signal contributes its configured
weight; the image is flagged as a screenshot when the summed confidence reaches
the configured threshold. Lowering :attr:`ScreenshotConfig.threshold` increases
sensitivity.

The pure :func:`classify_screenshot` function operates on already-extracted
signals and needs no third-party dependency. :func:`detect_screenshot` is a
convenience wrapper that reads those signals from a file using Pillow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from winnow.classify._image import extract_dimensions, extract_exif, open_image

SCREENSHOT_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"screen[\s_-]?shot", re.IGNORECASE),
    re.compile(r"screen[\s_-]?capture", re.IGNORECASE),
    re.compile(r"scr(?:n|een)?cap\b", re.IGNORECASE),
    re.compile(r"\bcapture[\s_-]?d?[\s_-]?ecran", re.IGNORECASE),
)
"""Regular expressions matched against candidate file names."""

DEFAULT_SCREENSHOT_SOFTWARE_MARKERS: frozenset[str] = frozenset(
    {
        "screenshot",
        "screen shot",
        "screencapture",
        "screencap",
        "snipping tool",
        "snip & sketch",
        "gnome-screenshot",
        "spectacle",
        "flameshot",
        "greenshot",
        "lightshot",
        "shutter",
    },
)
"""Default case-insensitive substrings that indicate a screen-capture tool.

Override or extend via :attr:`ScreenshotConfig.software_markers`, for example
``ScreenshotConfig(software_markers=DEFAULT_SCREENSHOT_SOFTWARE_MARKERS | {"mytool"})``.
"""

DEFAULT_DISPLAY_ASPECT_RATIOS: frozenset[Fraction] = frozenset(
    {
        Fraction(5, 4),
        Fraction(4, 3),
        Fraction(3, 2),
        Fraction(16, 10),
        Fraction(16, 9),
        Fraction(19, 9),
        Fraction(13, 6),
        Fraction(20, 9),
        Fraction(43, 18),
        Fraction(64, 27),
    },
)
"""Default display panel aspect ratios (landscape-normalized).

Covers classic desktop panels (5:4, 4:3, 3:2, 16:10, 16:9), tall phone panels
(19:9, 19.5:9 as 13:6, 20:9), and ultrawides (43:18 and 64:27, the panel ratios
marketed as 21:9). Override or extend via :attr:`ScreenshotConfig.aspect_ratios`.
"""


@dataclass(frozen=True, slots=True)
class ScreenshotConfig:
    """Tunable weights and threshold for screenshot detection.

    Attributes:
        filename_weight: Confidence added when the file name matches a known
            screenshot pattern.
        software_weight: Confidence added when EXIF software names a capture tool.
        dimension_weight: Confidence added when the dimensions look like a screen
            resolution. Kept deliberately below ``threshold``: many photos share
            display aspect ratios, so dimensions only corroborate other signals.
        threshold: Minimum summed confidence, in ``[0.0, 1.0]``, required to flag
            an image as a screenshot. Lower values increase sensitivity.
        software_markers: Case-insensitive substrings that mark an EXIF
            ``Software`` value as a screen-capture tool. Defaults to
            :data:`DEFAULT_SCREENSHOT_SOFTWARE_MARKERS`; extend with
            ``DEFAULT_SCREENSHOT_SOFTWARE_MARKERS | {"mytool"}``.
        aspect_ratios: Landscape-normalized display aspect ratios that make
            dimensions look screen-like. Defaults to
            :data:`DEFAULT_DISPLAY_ASPECT_RATIOS`.
        aspect_ratio_tolerance: Maximum relative deviation from a known aspect
            ratio for dimensions to count as a match. The default covers panels
            that only approximate their marketed ratio (for example 1366x768).
        min_screen_edge: Minimum shorter-edge length, in pixels, for dimensions
            to be considered a screen resolution. Filters out thumbnails and
            other small images that happen to share a display ratio.
        extra_resolutions: Exact ``(width, height)`` resolutions to treat as
            screen sizes, in either orientation, regardless of aspect ratio or
            size checks.
    """

    filename_weight: float = 0.6
    software_weight: float = 0.8
    dimension_weight: float = 0.3
    threshold: float = 0.5
    software_markers: frozenset[str] = field(
        default_factory=lambda: DEFAULT_SCREENSHOT_SOFTWARE_MARKERS,
    )
    aspect_ratios: frozenset[Fraction] = field(
        default_factory=lambda: DEFAULT_DISPLAY_ASPECT_RATIOS,
    )
    aspect_ratio_tolerance: float = 0.01
    min_screen_edge: int = 720
    extra_resolutions: frozenset[tuple[int, int]] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ScreenshotSignals:
    """Individual heuristics that fired during screenshot detection.

    Attributes:
        filename_match: Whether the file name matched a screenshot pattern.
        software_match: Whether EXIF software named a capture tool.
        dimension_match: Whether the dimensions matched a common screen size.
    """

    filename_match: bool
    software_match: bool
    dimension_match: bool


@dataclass(frozen=True, slots=True)
class ScreenshotClassification:
    """Result of screenshot detection.

    Attributes:
        is_screenshot: Whether the confidence reached the configured threshold.
        confidence: Summed signal confidence, clamped to ``[0.0, 1.0]``.
        signals: The individual signals that contributed to the score.
    """

    is_screenshot: bool
    confidence: float
    signals: ScreenshotSignals


def classify_screenshot(
    *,
    filename: str | None = None,
    software: str | None = None,
    dimensions: tuple[int, int] | None = None,
    config: ScreenshotConfig | None = None,
) -> ScreenshotClassification:
    """Classify an image as a screenshot from pre-extracted signals.

    Args:
        filename: File name (or path) to match against screenshot patterns.
        software: EXIF ``Software`` value, if any.
        dimensions: ``(width, height)`` pixel dimensions, if known.
        config: Detection weights and threshold. Defaults to
            :class:`ScreenshotConfig`.

    Returns:
        A :class:`ScreenshotClassification` describing the decision.
    """
    active_config = config if config is not None else ScreenshotConfig()

    signals = ScreenshotSignals(
        filename_match=_matches_filename(filename),
        software_match=_matches_software(
            software=software,
            config=active_config,
        ),
        dimension_match=_matches_resolution(
            dimensions=dimensions,
            config=active_config,
        ),
    )

    confidence = 0.0
    if signals.filename_match:
        confidence += active_config.filename_weight
    if signals.software_match:
        confidence += active_config.software_weight
    if signals.dimension_match:
        confidence += active_config.dimension_weight
    confidence = min(1.0, confidence)

    return ScreenshotClassification(
        is_screenshot=confidence >= active_config.threshold,
        confidence=confidence,
        signals=signals,
    )


def detect_screenshot(
    path: Path,
    *,
    config: ScreenshotConfig | None = None,
) -> ScreenshotClassification:
    """Detect whether an image file is a screenshot.

    Reads dimensions and EXIF software from the file using Pillow, then applies
    :func:`classify_screenshot`.

    Args:
        path: Filesystem path to the image.
        config: Detection weights and threshold. Defaults to
            :class:`ScreenshotConfig`.

    Returns:
        A :class:`ScreenshotClassification` describing the decision.

    Raises:
        MediaError: If the file cannot be read as an image.
    """
    with open_image(path) as image:
        dimensions = extract_dimensions(image)
        exif = extract_exif(image)
    software = exif.get("Software")

    return classify_screenshot(
        filename=path.name,
        software=software,
        dimensions=dimensions,
        config=config,
    )


def _matches_filename(filename: str | None) -> bool:
    """Return whether a file name matches a known screenshot pattern.

    Args:
        filename: File name or path to inspect.

    Returns:
        ``True`` when any screenshot filename pattern matches.
    """
    if not filename:
        return False
    name = Path(filename).name
    return any(pattern.search(name) for pattern in SCREENSHOT_FILENAME_PATTERNS)


def _matches_software(
    *,
    software: str | None,
    config: ScreenshotConfig,
) -> bool:
    """Return whether EXIF software indicates a screen-capture tool.

    Args:
        software: EXIF ``Software`` value.
        config: Detection config providing the capture-tool markers.

    Returns:
        ``True`` when the value contains a configured capture-tool marker.
    """
    if not software:
        return False
    normalized = software.casefold()
    return any(marker in normalized for marker in config.software_markers)


def _matches_resolution(
    *,
    dimensions: tuple[int, int] | None,
    config: ScreenshotConfig,
) -> bool:
    """Return whether dimensions look like a screen resolution.

    Dimensions match when they equal a configured extra resolution in either
    orientation, or when the image is at least ``min_screen_edge`` pixels on its
    shorter edge and its landscape-normalized aspect ratio falls within
    ``aspect_ratio_tolerance`` of a configured display aspect ratio.

    Args:
        dimensions: ``(width, height)`` pixel dimensions.
        config: Detection config providing aspect ratios, tolerance, minimum
            edge, and any extra exact resolutions.

    Returns:
        ``True`` when the dimensions look like a screen size.
    """
    if dimensions is None:
        return False
    width, height = dimensions
    if width <= 0 or height <= 0:
        return False

    swapped = (height, width)
    if dimensions in config.extra_resolutions or swapped in config.extra_resolutions:
        return True

    long_edge, short_edge = max(width, height), min(width, height)
    if short_edge < config.min_screen_edge:
        return False

    ratio = long_edge / short_edge
    return any(
        abs(ratio - float(known_ratio))
        <= float(known_ratio) * config.aspect_ratio_tolerance
        for known_ratio in config.aspect_ratios
    )
