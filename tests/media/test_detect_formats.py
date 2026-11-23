"""Every extension the default registry claims detects as expected (#393).

Samples are produced at test time: Pillow for raster images, pillow-heif for
HEIC (skipped when it cannot encode), ffmpeg for audio and video (skipped
without it), and hand-built headers for RAW formats without a real sample,
marked ``header-only`` in the parameter id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from tests.media.detect_support import (
    AUDIO_IN,
    CR3_PROFILE,
    FFMPEG,
    FFPROBE,
    VIDEO_IN,
    record_dispatch,
    run_ffmpeg,
    save_heif,
    write_canon,
)
from winnow.media.registry import RAW_IMAGE_MIME_TYPES, detect_media_type
from winnow.media.service import DefaultMetadataService
from winnow.models.media import MediaType


@dataclass(frozen=True)
class _Sample:
    """How to write one sample and what it must detect as.

    Args:
        extension: File extension.
        expected: Expected media type.
        write: Writes the sample to the given path.
        needs: Tool the sample needs (``"ffmpeg"``, ``"ffprobe"``, ``"heif"``
            or ``""``); Matroska also needs ffprobe to be typed.
    """

    extension: str
    expected: MediaType
    write: Callable[[Path], None]
    needs: str = ""


def _pillow(fmt: str) -> Callable[[Path], None]:
    """Return a writer that saves a tiny RGB image with Pillow.

    Args:
        fmt: Pillow format name.

    Returns:
        The writer.
    """

    def write(path: Path) -> None:
        try:
            Image.new("RGB", (8, 6), (120, 60, 30)).save(path, format=fmt)
        except (KeyError, OSError) as exc:
            pytest.skip(f"Pillow cannot write {fmt} here: {exc}")

    return write


def _heif(path: Path) -> None:
    """Write a tiny HEIF with pillow-heif.

    Args:
        path: Destination.
    """
    save_heif(path, Image.new("RGB", (8, 6), (1, 2, 3)))


def _ff(*args: str) -> Callable[[Path], None]:
    """Return a writer that runs ffmpeg with ``args`` then the path.

    Args:
        args: Inputs, codecs and format options.

    Returns:
        The writer.
    """

    def write(path: Path) -> None:
        run_ffmpeg(*args, str(path))

    return write


def _header(data: bytes) -> Callable[[Path], None]:
    """Return a writer for a header-only RAW sample.

    Args:
        data: Leading bytes of the format.

    Returns:
        The writer.
    """

    def write(path: Path) -> None:
        path.write_bytes(data + b"\x00" * 64)

    return write


def _cr3(path: Path) -> None:
    """Write a bounded synthetic CR3.

    Args:
        path: Destination.
    """
    write_canon(path, CR3_PROFILE)


_TIFF_HEADER = b"II*\x00\x08\x00\x00\x00"
_RAW_HEADERS: dict[str, bytes] = {
    "orf": b"IIRO\x08\x00\x00\x00",
    "rw2": b"IIU\x00\x18\x00\x00\x00",
    "raw": b"IIU\x00\x18\x00\x00\x00",
    "rwl": b"IIU\x00\x18\x00\x00\x00",
    "raf": b"FUJIFILMCCD-RAW 0201FF383501",
    "mrw": b"\x00MRM\x00\x00\x00\x00",
    # Sigma X3F magic (F, O, V, b) in hex, which the spell checker leaves alone.
    "x3f": bytes.fromhex("464f5662") + b"\x00" * 4,
    "ari": b"ARRI\x12\x34\x56\x78",
}

_MAINSTREAM: tuple[_Sample, ...] = (
    _Sample("jpg", MediaType.IMAGE, _pillow("JPEG")),
    _Sample("jpeg", MediaType.IMAGE, _pillow("JPEG")),
    _Sample("png", MediaType.IMAGE, _pillow("PNG")),
    _Sample("gif", MediaType.IMAGE, _pillow("GIF")),
    _Sample("bmp", MediaType.IMAGE, _pillow("BMP")),
    _Sample("tif", MediaType.IMAGE, _pillow("TIFF")),
    _Sample("tiff", MediaType.IMAGE, _pillow("TIFF")),
    _Sample("webp", MediaType.IMAGE, _pillow("WEBP")),
    _Sample("avif", MediaType.IMAGE, _pillow("AVIF")),
    _Sample("heic", MediaType.IMAGE, _heif, "heif"),
    _Sample("heif", MediaType.IMAGE, _heif, "heif"),
    _Sample("mp4", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample("mov", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample("m4v", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample("3gp", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "h263"), "ffmpeg"),
    _Sample("3g2", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "h263"), "ffmpeg"),
    _Sample("mkv", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "mpeg4"), "ffprobe"),
    _Sample("webm", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "libvpx"), "ffprobe"),
    _Sample("avi", MediaType.VIDEO, _ff(*VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample(
        "mts",
        MediaType.VIDEO,
        _ff(*VIDEO_IN, "-c:v", "mpeg4", "-f", "mpegts"),
        "ffmpeg",
    ),
    _Sample(
        "m2ts",
        MediaType.VIDEO,
        _ff(*VIDEO_IN, "-c:v", "mpeg4", "-f", "mpegts", "-mpegts_m2ts_mode", "1"),
        "ffmpeg",
    ),
    _Sample("mp3", MediaType.AUDIO, _ff(*AUDIO_IN, "-c:a", "libmp3lame"), "ffmpeg"),
    _Sample("m4a", MediaType.AUDIO, _ff(*AUDIO_IN, "-c:a", "aac"), "ffmpeg"),
    _Sample(
        "m4b",
        MediaType.AUDIO,
        _ff(*AUDIO_IN, "-c:a", "aac", "-f", "ipod"),
        "ffmpeg",
    ),
    _Sample("flac", MediaType.AUDIO, _ff(*AUDIO_IN, "-c:a", "flac"), "ffmpeg"),
    _Sample("wav", MediaType.AUDIO, _ff(*AUDIO_IN, "-c:a", "pcm_s16le"), "ffmpeg"),
    _Sample("aiff", MediaType.AUDIO, _ff(*AUDIO_IN, "-c:a", "pcm_s16be"), "ffmpeg"),
    _Sample(
        "ogg",
        MediaType.AUDIO,
        _ff(*AUDIO_IN, "-c:a", "libopus", "-f", "ogg"),
        "ffmpeg",
    ),
    _Sample("opus", MediaType.AUDIO, _ff(*AUDIO_IN, "-c:a", "libopus"), "ffmpeg"),
    _Sample(
        "aac",
        MediaType.AUDIO,
        _ff(*AUDIO_IN, "-c:a", "aac", "-f", "adts"),
        "ffmpeg",
    ),
)

_RAW: tuple[_Sample, ...] = tuple(
    _Sample(
        extension,
        MediaType.IMAGE,
        (
            _cr3
            if extension == "cr3"
            else _header(_RAW_HEADERS.get(extension, _TIFF_HEADER))
        ),
    )
    for extension in sorted(RAW_IMAGE_MIME_TYPES)
)


def _sample_id(sample: _Sample) -> str:
    """Name a sample row; header-only RAW rows say so.

    Args:
        sample: The row.

    Returns:
        The parameter id.
    """
    if sample in _RAW:
        return f"{sample.extension}-header-only"
    return sample.extension


@pytest.mark.parametrize("sample", (*_MAINSTREAM, *_RAW), ids=_sample_id)
def test_detect_every_registry_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample: _Sample,
) -> None:
    """Every claimed extension detects as expected and reaches its extractor."""
    if sample.needs in {"ffmpeg", "ffprobe"} and FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    if sample.needs == "ffprobe" and FFPROBE is None:
        pytest.skip("Matroska stream types need ffprobe")
    target = tmp_path / f"sample.{sample.extension}"
    sample.write(target)

    assert_that(detect_media_type(target)).is_equal_to(sample.expected)

    dispatched = record_dispatch(monkeypatch)
    DefaultMetadataService().extract(target)

    assert_that(dispatched).is_equal_to([sample.expected])


def test_registry_rows_cover_raw_supplement() -> None:
    """The registry test covers every RAW extension the registry claims."""
    covered = {sample.extension for sample in _RAW}

    assert_that(covered).is_equal_to(set(RAW_IMAGE_MIME_TYPES))
