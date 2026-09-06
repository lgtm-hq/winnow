"""Image metadata extraction, EXIF parsing, and thumbnail generation.

Covers common raster formats (JPEG, PNG, WEBP, TIFF, GIF, BMP) plus HEIF/HEIC
when the optional ``pillow-heif`` codec can be loaded. RAW formats are handled on
a best-effort basis: when Pillow cannot decode the pixels, dimensions are
recovered from embedded EXIF tags where possible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import exifread
from loguru import logger
from PIL import Image, UnidentifiedImageError

from winnow.exceptions import MediaError
from winnow.models.media import MediaMetadata

_HEIF_AVAILABLE: bool = False
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - optional codec, any failure degrades
    logger.debug("pillow-heif unavailable; HEIF/HEIC support disabled: {}", exc)

DEFAULT_THUMBNAIL_SIZE: Final[tuple[int, int]] = (256, 256)

_EXIF_WIDTH_TAGS: Final[tuple[str, ...]] = (
    "EXIF ExifImageWidth",
    "Image ImageWidth",
)
_EXIF_HEIGHT_TAGS: Final[tuple[str, ...]] = (
    "EXIF ExifImageLength",
    "Image ImageLength",
)

_MODE_BIT_DEPTH: Final[dict[str, int]] = {
    "1": 1,
    "L": 8,
    "LA": 8,
    "La": 8,
    "P": 8,
    "PA": 8,
    "RGB": 8,
    "RGBX": 8,
    "RGBA": 8,
    "RGBa": 8,
    "CMYK": 8,
    "YCbCr": 8,
    "LAB": 8,
    "HSV": 8,
    "I;16": 16,
    "I;16L": 16,
    "I;16B": 16,
    "I;16N": 16,
    "I": 32,
    "F": 32,
}


def heif_supported() -> bool:
    """Report whether HEIF/HEIC decoding is available.

    Returns:
        ``True`` when the optional ``pillow-heif`` codec loaded successfully.
    """
    return _HEIF_AVAILABLE


def extract_image_metadata(path: Path) -> MediaMetadata:
    """Extract structural metadata from an image file.

    Args:
        path: Filesystem path to the image.

    Returns:
        Metadata populated with dimensions, format, color mode, bit depth, and
        alpha presence. RAW files that Pillow cannot decode return partial
        metadata recovered from EXIF when available.

    Raises:
        MediaError: If the file is missing, unreadable, or cannot be decoded and
            has no usable EXIF fallback.
    """
    if not path.is_file():
        raise MediaError(
            "image file does not exist",
            operation="extract_image_metadata",
            file_path=path,
        )

    try:
        with Image.open(path) as image:
            width, height = image.size
            return MediaMetadata(
                width=width,
                height=height,
                image_format=image.format,
                color_mode=image.mode,
                bit_depth=_mode_bit_depth(image.mode),
                has_alpha=_mode_has_alpha(image=image),
            )
    except UnidentifiedImageError:
        logger.debug("Pillow could not identify {}; trying EXIF fallback", path)
        return _metadata_from_exif(path=path)
    except (OSError, ValueError) as exc:
        raise MediaError(
            "failed to read image",
            operation="extract_image_metadata",
            file_path=path,
        ) from exc


def read_exif(path: Path) -> dict[str, str]:
    """Read EXIF tags from an image file.

    Thumbnail and MakerNote binary blobs are skipped to keep the result
    JSON-friendly. Failures degrade to an empty mapping rather than raising, so
    callers can treat missing EXIF as a normal, expected outcome.

    Args:
        path: Filesystem path to the image.

    Returns:
        Mapping of EXIF tag name to its stringified value. Empty when the file
        has no EXIF or cannot be parsed.
    """
    try:
        with path.open("rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except Exception as exc:  # noqa: BLE001 - exifread may raise struct.error etc.
        logger.debug("EXIF read failed for {}: {}", path, exc)
        return {}

    return {
        name: str(value)
        for name, value in tags.items()
        if name != "JPEGThumbnail" and not name.startswith("MakerNote")
    }


_MAKER_NOTE_PREFIX: Final[str] = "MakerNote "


def read_maker_note_tags(path: Path) -> dict[str, str]:
    """Read decoded MakerNote tags from an image file.

    Unlike :func:`read_exif`, exifread runs with ``details=True`` so vendor
    MakerNote IFDs (for example Apple's, which carries the Live Photo content
    identifier in ``Tag 0x0011``) are decoded. Only MakerNote keys are
    returned, with the ``"MakerNote "`` prefix stripped. Failures degrade to an
    empty mapping, matching the :func:`read_exif` policy.

    Args:
        path: Filesystem path to the image.

    Returns:
        Mapping of MakerNote tag name (for example ``"Tag 0x0011"``) to its
        stringified value. Empty when the file has no MakerNote or cannot be
        parsed.
    """
    try:
        with path.open("rb") as handle:
            tags = exifread.process_file(handle, details=True)
    except Exception as exc:  # noqa: BLE001 - exifread may raise struct.error etc.
        logger.debug("MakerNote read failed for {}: {}", path, exc)
        return {}

    return {
        name.removeprefix(_MAKER_NOTE_PREFIX): str(value)
        for name, value in tags.items()
        if name.startswith(_MAKER_NOTE_PREFIX)
    }


def generate_thumbnail(
    path: Path,
    destination: Path,
    *,
    size: tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
) -> Path:
    """Generate a downscaled thumbnail for an image.

    The aspect ratio is preserved; ``size`` is treated as a bounding box. The
    output format is inferred from the destination suffix.

    Args:
        path: Source image path.
        destination: Path where the thumbnail is written.
        size: Maximum ``(width, height)`` bounding box for the thumbnail.

    Returns:
        The destination path that was written.

    Raises:
        MediaError: If the source cannot be decoded or the thumbnail cannot be
            written.
    """
    try:
        with Image.open(path) as image:
            thumbnail = image.copy()
            thumbnail.thumbnail(size)
            destination.parent.mkdir(parents=True, exist_ok=True)
            thumbnail.save(destination)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise MediaError(
            "failed to generate thumbnail",
            operation="generate_thumbnail",
            file_path=path,
            details={"destination": str(destination)},
        ) from exc
    return destination


def _metadata_from_exif(path: Path) -> MediaMetadata:
    """Recover partial image metadata from EXIF when decoding fails.

    Args:
        path: Filesystem path to the image.

    Returns:
        Metadata with dimensions filled from EXIF where present.

    Raises:
        MediaError: If no usable EXIF dimensions are available.
    """
    tags = read_exif(path)
    width = _first_int_tag(tags=tags, names=_EXIF_WIDTH_TAGS)
    height = _first_int_tag(tags=tags, names=_EXIF_HEIGHT_TAGS)
    if width is None and height is None:
        raise MediaError(
            "unsupported or corrupt image",
            operation="extract_image_metadata",
            file_path=path,
        )
    return MediaMetadata(
        width=width,
        height=height,
        image_format=_format_from_suffix(path),
    )


def _format_from_suffix(path: Path) -> str | None:
    """Derive an uppercase format label from a file suffix.

    TODO(#155): route this EXIF-fallback format inference through the
    content-sniffing detection layer once it lands, instead of trusting
    the file suffix.

    Keeps the EXIF fallback consistent with Pillow, which reports uppercase
    format names such as ``"JPEG"`` or ``"TIFF"``.

    Args:
        path: Filesystem path whose suffix identifies the format.

    Returns:
        Uppercase format label, or ``None`` when the path has no suffix.
    """
    return path.suffix.lstrip(".").upper() or None


def _first_int_tag(*, tags: dict[str, str], names: tuple[str, ...]) -> int | None:
    """Return the first parseable positive integer among candidate EXIF tags.

    Args:
        tags: Stringified EXIF tag mapping.
        names: Ordered candidate tag names to inspect.

    Returns:
        Parsed integer value, or ``None`` when no candidate is a valid integer.
    """
    for name in names:
        raw = tags.get(name)
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def _mode_bit_depth(mode: str) -> int | None:
    """Compute the bit depth per channel for a Pillow image mode.

    Uses a static mapping of documented Pillow modes rather than the
    non-public ``PIL.ImageMode`` internals.

    Args:
        mode: Pillow image mode string (for example ``"RGB"`` or ``"I;16"``).

    Returns:
        Bits per channel, or ``None`` when the mode is not recognized.
    """
    return _MODE_BIT_DEPTH.get(mode)


def _mode_has_alpha(*, image: Image.Image) -> bool:
    """Report whether an image carries an alpha or transparency channel.

    Args:
        image: Opened Pillow image.

    Returns:
        ``True`` when the image has an alpha band or transparency metadata.
    """
    return "A" in image.getbands() or "transparency" in image.info
