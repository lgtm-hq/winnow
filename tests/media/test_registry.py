"""Tests for media format registry behavior."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from winnow.media import (
    DEFAULT_FORMAT_REGISTRY,
    DEFAULT_FORMATS,
    FormatRegistry,
    create_default_format_registry,
    media_type_for_extension,
    normalize_extension,
)
from winnow.models.media import MediaType

COMMON_FORMAT_CASES: list[tuple[str, MediaType]] = [
    ("jpeg", MediaType.IMAGE),
    ("jpg", MediaType.IMAGE),
    ("png", MediaType.IMAGE),
    ("heic", MediaType.IMAGE),
    ("heif", MediaType.IMAGE),
    ("webp", MediaType.IMAGE),
    ("tiff", MediaType.IMAGE),
    ("tif", MediaType.IMAGE),
    ("bmp", MediaType.IMAGE),
    ("gif", MediaType.IMAGE),
    ("3fr", MediaType.IMAGE),
    ("ari", MediaType.IMAGE),
    ("arw", MediaType.IMAGE),
    ("cr2", MediaType.IMAGE),
    ("cr3", MediaType.IMAGE),
    ("dcr", MediaType.IMAGE),
    ("dng", MediaType.IMAGE),
    ("erf", MediaType.IMAGE),
    ("k25", MediaType.IMAGE),
    ("kdc", MediaType.IMAGE),
    ("mrw", MediaType.IMAGE),
    ("nef", MediaType.IMAGE),
    ("nrw", MediaType.IMAGE),
    ("orf", MediaType.IMAGE),
    ("pef", MediaType.IMAGE),
    ("raf", MediaType.IMAGE),
    ("raw", MediaType.IMAGE),
    ("rw2", MediaType.IMAGE),
    ("rwl", MediaType.IMAGE),
    ("sr2", MediaType.IMAGE),
    ("srf", MediaType.IMAGE),
    ("srw", MediaType.IMAGE),
    ("x3f", MediaType.IMAGE),
    ("mp4", MediaType.VIDEO),
    ("mov", MediaType.VIDEO),
    ("avi", MediaType.VIDEO),
    ("mkv", MediaType.VIDEO),
    ("webm", MediaType.VIDEO),
    ("mp3", MediaType.AUDIO),
    ("flac", MediaType.AUDIO),
    ("wav", MediaType.AUDIO),
    ("aac", MediaType.AUDIO),
    ("ogg", MediaType.AUDIO),
]


@pytest.mark.parametrize(("extension", "expected_media_type"), COMMON_FORMAT_CASES)
def test_default_registry_maps_common_formats(
    extension: str,
    expected_media_type: MediaType,
) -> None:
    """Default registry maps required common media formats."""
    registry = create_default_format_registry()

    assert_that(registry.lookup(extension)).is_equal_to(expected_media_type)


def test_default_format_registry_exports_all_default_formats() -> None:
    """Shared public registry exposes every default extension mapping."""
    assert_that(DEFAULT_FORMAT_REGISTRY.formats).contains_key(*DEFAULT_FORMATS.keys())


@pytest.mark.parametrize(
    ("extension", "expected_extension", "expected_media_type"),
    [
        (".JpEg", "jpeg", MediaType.IMAGE),
        ("Vacation.MP4", "mp4", MediaType.VIDEO),
        (".hidden.MP4", "mp4", MediaType.VIDEO),
        ("/media/family/SONG.FLAC", "flac", MediaType.AUDIO),
    ],
)
def test_lookup_normalizes_case_dots_file_names_and_paths(
    extension: str,
    expected_extension: str,
    expected_media_type: MediaType,
) -> None:
    """Lookup accepts case-insensitive extensions, file names, and paths."""
    assert_that(normalize_extension(extension)).is_equal_to(expected_extension)
    assert_that(media_type_for_extension(extension)).is_equal_to(expected_media_type)


def test_custom_extension_can_be_registered() -> None:
    """Custom extension registration adds a new mapping."""
    registry = FormatRegistry(include_defaults=False)

    registry.register(extension=".avif", media_type=MediaType.IMAGE)

    assert_that(registry.lookup("AVIF")).is_equal_to(MediaType.IMAGE)
    assert_that(registry.formats).contains_entry({"avif": MediaType.IMAGE})


def test_registry_can_be_created_from_config_mapping() -> None:
    """Config-style mappings can extend or replace default formats."""
    registry = FormatRegistry.from_config(
        {
            "formats": {
                "m4a": "audio",
                ".still": "IMAGE",
                "clip": MediaType.VIDEO,
            },
            "include_defaults": False,
            "use_mime_fallback": False,
        },
    )

    assert_that(registry.lookup("m4a")).is_equal_to(MediaType.AUDIO)
    assert_that(registry.lookup(".still")).is_equal_to(MediaType.IMAGE)
    assert_that(registry.lookup("clip")).is_equal_to(MediaType.VIDEO)
    assert_that(registry.lookup("jpg")).is_none()


def test_unknown_extensions_return_none() -> None:
    """Lookup returns None for unknown extensions instead of raising."""
    registry = create_default_format_registry()

    assert_that(registry.lookup("not-a-real-media-format")).is_none()
    assert_that(registry.lookup("")).is_none()
    assert_that(registry.lookup("pdf")).is_none()


def test_mime_fallback_can_infer_unregistered_media_extension() -> None:
    """MIME fallback can classify unregistered image, video, or audio suffixes."""
    fallback_registry = FormatRegistry(include_defaults=False)
    strict_registry = FormatRegistry(
        include_defaults=False,
        use_mime_fallback=False,
    )

    assert_that(fallback_registry.lookup("svg")).is_equal_to(MediaType.IMAGE)
    assert_that(strict_registry.lookup("svg")).is_none()


def test_register_rejects_empty_extension() -> None:
    """Registering an empty extension fails with a clear error."""
    registry = FormatRegistry(include_defaults=False)

    with pytest.raises(ValueError, match="extension must contain"):
        registry.register(extension="...", media_type=MediaType.IMAGE)


def test_from_config_rejects_unknown_media_type() -> None:
    """Config mappings must use a supported media type value."""
    with pytest.raises(ValueError, match="unsupported media type"):
        FormatRegistry.from_config(
            {
                "formats": {"mystery": "document"},
                "include_defaults": False,
            },
        )
