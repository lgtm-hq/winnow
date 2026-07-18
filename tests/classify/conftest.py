"""Shared fixtures for classifier tests.

Fixtures generate tiny images on disk so the file-based classifiers can be
exercised without committing binary assets to the repository.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from PIL import ExifTags, Image

ImageFactory = Callable[..., Path]


@pytest.fixture
def make_image(tmp_path: Path) -> ImageFactory:
    """Return a factory that writes tiny image files for tests.

    Returns:
        A callable that creates an image file and returns its path.
    """

    def _make_image(
        *,
        name: str = "image.png",
        size: tuple[int, int] = (16, 16),
        mode: str = "RGB",
        colors: Sequence[tuple[int, ...]] | None = None,
        exif: Mapping[int, object] | None = None,
        exif_ifd: Mapping[int, object] | None = None,
    ) -> Path:
        """Create an image file.

        Args:
            name: File name to write within the temporary directory.
            size: ``(width, height)`` pixel dimensions.
            mode: Pillow image mode (for example ``"RGB"`` or ``"RGBA"``).
            colors: Optional per-pixel colors; cycled to fill the image. When
                omitted a single flat color is used.
            exif: Optional mapping of numeric base-IFD EXIF tag ids to values.
            exif_ifd: Optional mapping of numeric EXIF sub-IFD tag ids to values.

        Returns:
            Path to the written image file.
        """
        width, height = size
        image = Image.new(mode, size)
        if colors is not None:
            pixel_count = width * height
            filled = [colors[index % len(colors)] for index in range(pixel_count)]
            image.putdata(filled)

        path = tmp_path / name
        if exif is None and exif_ifd is None:
            image.save(path)
            return path

        exif_data = Image.Exif()
        for tag_id, value in (exif or {}).items():
            exif_data[tag_id] = value
        if exif_ifd is not None:
            exif_data[ExifTags.IFD.Exif] = dict(exif_ifd)
        image.save(path, exif=exif_data)
        return path

    return _make_image


@pytest.fixture
def gradient_colors() -> list[tuple[int, int, int]]:
    """Return a rich set of distinct RGB colors resembling a photograph.

    Returns:
        A list of unique RGB tuples exceeding the graphic color threshold.
    """
    return [
        (red % 256, green % 256, (red * green) % 256)
        for red in range(0, 64)
        for green in range(0, 8)
    ]
