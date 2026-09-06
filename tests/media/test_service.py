"""Tests for the unified metadata extraction service."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import MediaError
from winnow.media import service as service_module
from winnow.media.registry import FormatRegistry, detect_media_type
from winnow.media.service import DefaultMetadataService, create_metadata_service
from winnow.models.config import WinnowConfig
from winnow.models.media import MediaMetadata, MediaType

_WHICH_TARGET = "winnow.media.video.shutil.which"


@pytest.fixture
def service() -> DefaultMetadataService:
    """Return a service backed by the default format registry.

    Returns:
        Freshly constructed ``DefaultMetadataService``.
    """
    return DefaultMetadataService()


def test_extract_dispatches_image(
    service: DefaultMetadataService,
    fixtures_dir: Path,
) -> None:
    """A JPEG fixture is routed to the image processor."""
    metadata = service.extract(fixtures_dir / "sample.jpg")

    assert_that(metadata.image_format).is_equal_to("JPEG")
    assert_that(metadata.width).is_greater_than(0)


def test_extract_dispatches_video_without_ffprobe(
    service: DefaultMetadataService,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MP4 fixture reaches the video processor and degrades without ffprobe."""
    monkeypatch.setattr(_WHICH_TARGET, lambda name: None)

    metadata = service.extract(fixtures_dir / "sample.mp4")

    assert_that(metadata).is_equal_to(MediaMetadata())


def test_extract_dispatches_audio(
    service: DefaultMetadataService,
    fixtures_dir: Path,
) -> None:
    """An MP3 fixture is routed to the audio processor."""
    metadata = service.extract(fixtures_dir / "sample.mp3")

    assert_that(metadata.duration_seconds).is_not_none()


def test_extract_rejects_directory(
    service: DefaultMetadataService,
    tmp_path: Path,
) -> None:
    """A directory is not a regular file and raises MediaError."""
    with pytest.raises(MediaError) as excinfo:
        service.extract(tmp_path)

    assert_that(excinfo.value.context.operation).is_equal_to("extract_metadata")
    assert_that(excinfo.value.context.file_path).is_equal_to(tmp_path)


def test_extract_rejects_empty_file(
    service: DefaultMetadataService,
    tmp_path: Path,
) -> None:
    """A zero-byte file raises MediaError before detection runs."""
    empty = tmp_path / "empty.jpg"
    empty.touch()

    with pytest.raises(MediaError) as excinfo:
        service.extract(empty)

    assert_that(excinfo.value.message).is_equal_to("empty file")
    assert_that(excinfo.value.context.operation).is_equal_to("extract_metadata")


def test_extract_rejects_undetectable_file(
    service: DefaultMetadataService,
    tmp_path: Path,
) -> None:
    """Unknown bytes with an unknown suffix raise a detection MediaError."""
    unknown = tmp_path / "blob.zzzunknown"
    unknown.write_bytes(b"\x00\x01\x02\x03 not any known media header")

    with pytest.raises(MediaError) as excinfo:
        service.extract(unknown)

    assert_that(excinfo.value.context.operation).is_equal_to("detect_media_type")


def test_extract_wraps_processor_failure(
    service: DefaultMetadataService,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-MediaError from a processor is wrapped with its cause preserved."""

    def _boom(path: Path) -> MediaMetadata:
        raise RuntimeError("processor exploded")

    monkeypatch.setattr(
        service_module,
        "_EXTRACTORS",
        {**service_module._EXTRACTORS, MediaType.IMAGE: _boom},
    )

    with pytest.raises(MediaError) as excinfo:
        service.extract(fixtures_dir / "sample.jpg")

    assert_that(excinfo.value.message).is_equal_to("metadata extraction failed")
    assert_that(excinfo.value.context.operation).is_equal_to("extract_metadata")
    assert_that(excinfo.value.context.details).is_equal_to(
        {"media_type": MediaType.IMAGE.value},
    )
    assert_that(excinfo.value.__cause__).is_instance_of(RuntimeError)


def test_extract_propagates_processor_media_error(
    service: DefaultMetadataService,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MediaError raised by a processor is re-raised unchanged."""
    original = MediaError("decode failed", operation="extract_image_metadata")

    def _raise(path: Path) -> MediaMetadata:
        raise original

    monkeypatch.setattr(
        service_module,
        "_EXTRACTORS",
        {**service_module._EXTRACTORS, MediaType.IMAGE: _raise},
    )

    with pytest.raises(MediaError) as excinfo:
        service.extract(fixtures_dir / "sample.jpg")

    assert_that(excinfo.value).is_same_as(original)


def test_extract_detects_exactly_once(
    service: DefaultMetadataService,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extract`` invokes ``detect_media_type`` a single time per file."""
    calls: list[Path] = []

    def _counting(
        path: Path,
        *,
        registry: FormatRegistry | None = None,
    ) -> MediaType | None:
        calls.append(path)
        return detect_media_type(path, registry=registry)

    monkeypatch.setattr(service_module, "detect_media_type", _counting)

    service.extract(fixtures_dir / "sample.jpg")

    assert_that(calls).is_length(1)


def test_detect_returns_media_type(
    service: DefaultMetadataService,
    fixtures_dir: Path,
) -> None:
    """``detect`` returns the classified media type for a known fixture."""
    assert_that(service.detect(fixtures_dir / "sample.mp3")).is_equal_to(
        MediaType.AUDIO,
    )


def test_extract_rejects_missing_file(
    service: DefaultMetadataService,
    tmp_path: Path,
) -> None:
    """A path that does not exist is reported as ``extract_metadata``."""
    with pytest.raises(MediaError, match="not a regular file") as error:
        service.extract(tmp_path / "missing.jpg")

    assert_that(error.value.context.operation).is_equal_to("extract_metadata")


def test_extract_wraps_filesystem_errors(
    service: DefaultMetadataService,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OSError`` while inspecting the path becomes a ``MediaError``."""

    def _denied(_self: Path) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", _denied)

    with pytest.raises(MediaError, match="cannot inspect file") as error:
        service.extract(fixtures_dir / "sample.jpg")

    assert_that(error.value.context.operation).is_equal_to("extract_metadata")
    assert_that(error.value.__cause__).is_instance_of(PermissionError)


def test_extract_rejects_media_type_without_processor(
    service: DefaultMetadataService,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A media type with no registered processor raises instead of ``KeyError``."""
    monkeypatch.setattr(service_module, "_EXTRACTORS", {})

    with pytest.raises(MediaError, match="no metadata processor") as error:
        service.extract(fixtures_dir / "sample.jpg")

    assert_that(error.value.context.details).is_equal_to({"media_type": "image"})


def test_detect_honours_injected_registry(fixtures_dir: Path) -> None:
    """An override registered on the injected registry wins over sniffing."""
    registry = FormatRegistry()
    registry.register(extension=".jpg", media_type=MediaType.AUDIO)
    custom = DefaultMetadataService(registry=registry)

    assert_that(custom.detect(fixtures_dir / "sample.jpg")).is_equal_to(
        MediaType.AUDIO,
    )
    assert_that(
        DefaultMetadataService().detect(fixtures_dir / "sample.jpg")
    ).is_equal_to(
        MediaType.IMAGE,
    )


def test_create_metadata_service_uses_isolated_registry(fixtures_dir: Path) -> None:
    """The factory's registry is a copy: shared-registry registrations do not leak."""
    created = create_metadata_service()
    shared = FormatRegistry()
    shared.register(extension=".jpg", media_type=MediaType.AUDIO)
    other = DefaultMetadataService(registry=shared)

    assert_that(created).is_instance_of(DefaultMetadataService)
    assert_that(created.detect(fixtures_dir / "sample.jpg")).is_equal_to(
        MediaType.IMAGE,
    )
    assert_that(other.detect(fixtures_dir / "sample.jpg")).is_equal_to(
        MediaType.AUDIO,
    )


def test_create_metadata_service_accepts_config(fixtures_dir: Path) -> None:
    """The factory accepts a config object and still extracts normally."""
    created = create_metadata_service(config=WinnowConfig())

    assert_that(created).is_instance_of(DefaultMetadataService)
    assert_that(created.extract(fixtures_dir / "sample.mp3")).is_instance_of(
        MediaMetadata,
    )
