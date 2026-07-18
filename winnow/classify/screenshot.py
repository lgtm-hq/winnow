"""Screenshot detection via EXIF, filename, and dimension heuristics.

Screenshots rarely carry camera EXIF metadata but frequently expose other tells:
a screen-capture tool recorded in the EXIF ``Software`` tag, a recognizable
filename pattern (for example ``Screenshot_20240101-120000.png`` or
``Screen Shot 2024-01-01 at 12.00.00.png``), and pixel dimensions that match a
common display or device resolution.

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
from pathlib import Path

from winnow.classify._image import extract_dimensions, extract_exif, open_image

SCREENSHOT_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"screen[\s_-]?shot", re.IGNORECASE),
    re.compile(r"screen[\s_-]?capture", re.IGNORECASE),
    re.compile(r"scr(?:n|een)?cap\b", re.IGNORECASE),
    re.compile(r"\bcapture[\s_-]?d?[\s_-]?ecran", re.IGNORECASE),
)
"""Regular expressions matched against candidate file names."""

SCREENSHOT_SOFTWARE_MARKERS: frozenset[str] = frozenset(
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
"""Case-insensitive substrings that indicate a screen-capture tool."""

COMMON_SCREEN_RESOLUTIONS: frozenset[tuple[int, int]] = frozenset(
    {
        (1024, 768),
        (1280, 720),
        (1280, 800),
        (1280, 1024),
        (1366, 768),
        (1440, 900),
        (1536, 864),
        (1600, 900),
        (1680, 1050),
        (1920, 1080),
        (1920, 1200),
        (2048, 1152),
        (2560, 1080),
        (2560, 1440),
        (2560, 1600),
        (2880, 1800),
        (3440, 1440),
        (3840, 2160),
        (750, 1334),
        (828, 1792),
        (1080, 1920),
        (1125, 2436),
        (1170, 2532),
        (1179, 2556),
        (1242, 2688),
        (1290, 2796),
        (1080, 2340),
        (1440, 2560),
        (1440, 3040),
    },
)
"""Common desktop and mobile screen resolutions (either orientation)."""


@dataclass(frozen=True, slots=True)
class ScreenshotConfig:
    """Tunable weights and threshold for screenshot detection.

    Attributes:
        filename_weight: Confidence added when the file name matches a known
            screenshot pattern.
        software_weight: Confidence added when EXIF software names a capture tool.
        dimension_weight: Confidence added when the dimensions match a common
            screen resolution.
        threshold: Minimum summed confidence, in ``[0.0, 1.0]``, required to flag
            an image as a screenshot. Lower values increase sensitivity.
        extra_resolutions: Additional ``(width, height)`` resolutions to treat as
            screen sizes, in either orientation.
    """

    filename_weight: float = 0.6
    software_weight: float = 0.8
    dimension_weight: float = 0.3
    threshold: float = 0.5
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
        software_match=_matches_software(software),
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
        MediaError: If Pillow is unavailable or the file cannot be read.
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


def _matches_software(software: str | None) -> bool:
    """Return whether EXIF software indicates a screen-capture tool.

    Args:
        software: EXIF ``Software`` value.

    Returns:
        ``True`` when the value contains a known capture-tool marker.
    """
    if not software:
        return False
    normalized = software.casefold()
    return any(marker in normalized for marker in SCREENSHOT_SOFTWARE_MARKERS)


def _matches_resolution(
    *,
    dimensions: tuple[int, int] | None,
    config: ScreenshotConfig,
) -> bool:
    """Return whether dimensions match a common screen resolution.

    Both orientations are considered.

    Args:
        dimensions: ``(width, height)`` pixel dimensions.
        config: Detection config providing any extra resolutions.

    Returns:
        ``True`` when the dimensions match a known screen size.
    """
    if dimensions is None:
        return False
    width, height = dimensions
    swapped = (height, width)
    known = COMMON_SCREEN_RESOLUTIONS | config.extra_resolutions
    return dimensions in known or swapped in known
