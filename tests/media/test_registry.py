"""Tests for media format registry and layered media-type detection."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.media import (
    DEFAULT_FORMAT_REGISTRY,
    DEFAULT_FORMATS,
    RAW_IMAGE_MIME_TYPES,
    FormatRegistry,
    create_default_format_registry,
    detect_media_type,
    media_type_for_extension,
    normalize_extension,
)
from winnow.models.media import MediaType

JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00" + b"\x00" * 20
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20
GIF_BYTES = b"GIF89a" + b"\x00" * 20
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16
MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 20
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20

REPRESENTATIVE_EXTENSION_CASES: list[tuple[str, MediaType]] = [
    ("jpg", MediaType.IMAGE),
    ("png", MediaType.IMAGE),
    ("heic", MediaType.IMAGE),
    ("nef", MediaType.IMAGE),
    ("x3f", MediaType.IMAGE),
    ("mp4", MediaType.VIDEO),
    ("mkv", MediaType.VIDEO),
    ("mp3", MediaType.AUDIO),
    ("flac", MediaType.AUDIO),
]


@pytest.mark.parametrize(
    ("extension", "expected_media_type"),
    REPRESENTATIVE_EXTENSION_CASES,
)
def test_default_registry_maps_representative_formats(
    extension: str,
    expected_media_type: MediaType,
) -> None:
    """Default registry resolves mainstream and RAW extensions by name."""
    registry = create_default_format_registry()

    assert_that(registry.lookup(extension)).is_equal_to(expected_media_type)


def test_default_formats_shrunk_to_raw_supplement() -> None:
    """DEFAULT_FORMATS only carries the RAW image supplement."""
    assert_that(set(DEFAULT_FORMATS)).is_equal_to(set(RAW_IMAGE_MIME_TYPES))
    assert_that(set(DEFAULT_FORMATS.values())).is_equal_to({MediaType.IMAGE})
    assert_that(DEFAULT_FORMATS).does_not_contain_key("jpg", "mp4", "mp3")


def test_raw_supplement_is_seeded_into_mimetypes() -> None:
    """Importing the registry seeds RAW extensions into ``mimetypes``."""
    for extension, expected_mime_type in RAW_IMAGE_MIME_TYPES.items():
        mime_type, _ = mimetypes.guess_type(f"photo.{extension}")

        assert_that(mime_type).described_as(extension).is_equal_to(
            expected_mime_type,
        )


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

    registry.register(extension=".fits", media_type=MediaType.IMAGE)

    assert_that(registry.lookup("FITS")).is_equal_to(MediaType.IMAGE)
    assert_that(registry.formats).contains_entry({"fits": MediaType.IMAGE})


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


def test_lookup_override_ignores_mime_fallback() -> None:
    """Override lookup only answers from explicit registrations."""
    registry = FormatRegistry(include_defaults=False)

    assert_that(registry.lookup_override("svg")).is_none()
    assert_that(registry.lookup("svg")).is_equal_to(MediaType.IMAGE)


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


def test_normalize_extension_returns_empty_for_suffixless_path() -> None:
    """A path without a suffix normalizes to an empty extension."""
    assert_that(normalize_extension("/media/family/SONG")).is_equal_to("")
    assert_that(normalize_extension("C:\\photos\\IMG")).is_equal_to("")
    assert_that(normalize_extension("C:\\photos.v1\\IMG")).is_equal_to("")


def test_lookup_returns_none_for_suffixless_path() -> None:
    """Looking up a suffix-less path yields no media type, not a MIME guess."""
    registry = create_default_format_registry()

    assert_that(registry.lookup("/media/family/SONG")).is_none()


def test_register_overrides_default_mapping() -> None:
    """A custom registration replaces the built-in mapping for an extension."""
    registry = create_default_format_registry()

    registry.register(extension="nef", media_type=MediaType.VIDEO)

    assert_that(registry.lookup("nef")).is_equal_to(MediaType.VIDEO)


@pytest.mark.parametrize(
    ("file_name", "content", "expected_media_type"),
    [
        ("photo.jpg", JPEG_BYTES, MediaType.IMAGE),
        ("graphic.png", PNG_BYTES, MediaType.IMAGE),
        ("loop.gif", GIF_BYTES, MediaType.IMAGE),
        ("clip.mp4", MP4_BYTES, MediaType.VIDEO),
        ("song.mp3", MP3_BYTES, MediaType.AUDIO),
        ("voice.wav", WAV_BYTES, MediaType.AUDIO),
    ],
)
def test_detect_media_type_sniffs_content(
    tmp_path: Path,
    file_name: str,
    content: bytes,
    expected_media_type: MediaType,
) -> None:
    """Content sniffing classifies real media headers."""
    media_file = tmp_path / file_name
    media_file.write_bytes(content)

    assert_that(detect_media_type(media_file)).is_equal_to(expected_media_type)


def test_detect_media_type_classifies_misnamed_file_by_content(
    tmp_path: Path,
) -> None:
    """JPEG bytes win over a misleading .txt extension."""
    misnamed = tmp_path / "notes.txt"
    misnamed.write_bytes(JPEG_BYTES)

    assert_that(detect_media_type(misnamed)).is_equal_to(MediaType.IMAGE)


def test_detect_media_type_classifies_extensionless_file_by_content(
    tmp_path: Path,
) -> None:
    """Content sniffing classifies files without any extension."""
    extensionless = tmp_path / "IMG0001"
    extensionless.write_bytes(PNG_BYTES)

    assert_that(detect_media_type(extensionless)).is_equal_to(MediaType.IMAGE)


def test_detect_media_type_override_beats_content(tmp_path: Path) -> None:
    """An explicit registry override outranks magic-byte sniffing."""
    registry = create_default_format_registry()
    registry.register(extension="txt", media_type=MediaType.AUDIO)
    overridden = tmp_path / "notes.txt"
    overridden.write_bytes(JPEG_BYTES)

    assert_that(
        detect_media_type(overridden, registry=registry),
    ).is_equal_to(MediaType.AUDIO)


def test_detect_media_type_falls_back_to_name_for_missing_file(
    tmp_path: Path,
) -> None:
    """Absent files fall back to name-based MIME inference."""
    missing = tmp_path / "vacation.mp4"

    assert_that(detect_media_type(missing)).is_equal_to(MediaType.VIDEO)


def test_detect_media_type_falls_back_to_name_for_unrecognized_content(
    tmp_path: Path,
) -> None:
    """Unrecognized bytes defer to the name-based fallback."""
    plain_text = tmp_path / "playlist.mp3"
    plain_text.write_bytes(b"just some plain text, no magic here\n")

    assert_that(detect_media_type(plain_text)).is_equal_to(MediaType.AUDIO)


def test_detect_media_type_returns_none_when_all_layers_fail(
    tmp_path: Path,
) -> None:
    """Unknown content with an unknown extension yields None."""
    unknown = tmp_path / "data.unknown-format"
    unknown.write_bytes(b"nothing recognizable in here\n")

    assert_that(detect_media_type(unknown)).is_none()


def test_sniff_skips_matches_without_mime_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """puremagic matches lacking a MIME type are skipped, not fatal."""
    target = tmp_path / "sample.bin"
    target.write_bytes(b"\x00\x01")

    class _Match:
        def __init__(self, mime_type: str | None) -> None:
            self.mime_type = mime_type

    monkeypatch.setattr(
        "winnow.media.registry.puremagic.magic_file",
        lambda _: [_Match(None), _Match(""), _Match("image/png")],
    )

    result = detect_media_type(target)

    assert_that(result).is_equal_to(MediaType.IMAGE)


def test_content_sniffing_outranks_builtin_raw_default(tmp_path: Path) -> None:
    """Built-in RAW defaults are name fallbacks, not user overrides."""
    misnamed_video = tmp_path / "clip.nef"
    misnamed_video.write_bytes(MP4_BYTES)

    assert_that(detect_media_type(misnamed_video)).is_equal_to(MediaType.VIDEO)


def test_user_override_outranks_content_for_raw_extension(tmp_path: Path) -> None:
    """An explicit user registration beats content sniffing."""
    registry = FormatRegistry()
    registry.register(extension="nef", media_type=MediaType.IMAGE)
    misnamed_video = tmp_path / "clip.nef"
    misnamed_video.write_bytes(MP4_BYTES)

    result = detect_media_type(misnamed_video, registry=registry)

    assert_that(result).is_equal_to(MediaType.IMAGE)


def test_raw_extension_still_resolves_by_name(tmp_path: Path) -> None:
    """RAW extensions with unrecognized content resolve as images by name."""
    raw_file = tmp_path / "shot.nef"
    raw_file.write_bytes(b"\x00\x01\x02\x03")

    assert_that(detect_media_type(raw_file)).is_equal_to(MediaType.IMAGE)
