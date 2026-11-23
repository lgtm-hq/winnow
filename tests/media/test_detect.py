"""Tests for content-based media-type detection: rule order and model (#393).

Rule 2 is tested in ``test_detect_bmff.py`` and ``test_detect_bmff_bounds.py``,
rule 4 in ``test_signatures.py``, rule 3 and transport streams in
``test_detect_streams.py``, every registry format in ``test_detect_formats.py``.
Cases that extract metadata use complete media: committed fixtures or files
written at test time by Pillow, pillow-heif, or ffmpeg (skipped without it).
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import puremagic
import pytest
from assertpy import assert_that

from tests.media import detect_support
from tests.media.bmff_support import write_ftyp
from tests.media.detect_support import (
    AUDIO_FIXTURES,
    AUDIO_IN,
    FIXTURES,
    LIBHEIF_BRANDS,
    VIDEO_IN,
    decision,
    details,
    detect_with_deadline,
    needs_ffmpeg,
    needs_ffprobe,
    run_ffmpeg,
)
from winnow.media import _detect
from winnow.media._detect import detect
from winnow.media.registry import create_default_format_registry, detect_media_type
from winnow.media.service import DefaultMetadataService
from winnow.models.media import MediaType


def _match(
    byte_match: bytes,
    extension: str,
    mime_type: str,
) -> puremagic.PureMagicWithConfidence:
    """Build a ``puremagic`` match.

    Args:
        byte_match: The bytes the match compared.
        extension: The match's extension.
        mime_type: The match's MIME type.

    Returns:
        The match.
    """
    return puremagic.main.PureMagicWithConfidence(
        byte_match=byte_match,
        offset=0,
        extension=extension,
        mime_type=mime_type,
        name=extension,
        confidence=0.5,
    )


def _pin_puremagic(
    monkeypatch: pytest.MonkeyPatch,
    *matches: puremagic.PureMagicWithConfidence,
) -> None:
    """Make ``puremagic`` report exactly ``matches``.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        matches: Matches to report.
    """
    monkeypatch.setattr(puremagic, "magic_stream", lambda _: list(matches))


_UNRECOGNISED = b"\x01\x02\x03\x04 binary bytes no rule recognises\x00\xfe"


# --- the bug reproductions ---------------------------------------------------


@pytest.mark.parametrize("name", ["sample.heic", "dated.heic"])
def test_detect_heic_fixture_is_image(name: str) -> None:
    """Both libheif-written HEIC fixtures are images."""
    result = detect(FIXTURES / name)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(result.major_brand).is_equal_to("heic")
    assert_that(result.compatible_brands).is_equal_to(LIBHEIF_BRANDS)
    assert_that(detect_media_type(FIXTURES / name)).is_equal_to(MediaType.IMAGE)


def test_detect_heic_ignores_puremagic_match_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """puremagic 2.2.0 listed video/mp4 first for the fixtures; it must not win."""
    _pin_puremagic(monkeypatch, _match(b"ftypheic", ".mp4", "video/mp4"))

    assert_that(detect_media_type(FIXTURES / "sample.heic")).is_equal_to(
        MediaType.IMAGE
    )


def test_service_extracts_heic_capture_date() -> None:
    """The HEIC fixture reaches the image reader and keeps its capture date."""
    metadata = DefaultMetadataService().extract(FIXTURES / "dated.heic")

    assert_that(metadata.captured_at).is_equal_to(datetime(2024, 3, 1, 12, 34, 56))
    assert_that(metadata.width).is_equal_to(32)
    assert_that(metadata.height).is_equal_to(32)


@needs_ffmpeg
@pytest.mark.parametrize("codec", ["aac", "alac"])
@pytest.mark.parametrize("name", ["tone.m4a", "tone.bin"])
def test_detect_m4a_is_audio(tmp_path: Path, codec: str, name: str) -> None:
    """Complete M4A files written by ffmpeg are audio, also when renamed."""
    written = tmp_path / "tone.m4a"
    run_ffmpeg(*AUDIO_IN, "-c:a", codec, str(written))
    target = written.rename(tmp_path / name)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(details(result)).contains("hdlr=soun", "major_brand=M4A ")


@needs_ffmpeg
def test_detect_audio_only_mp4_is_audio(tmp_path: Path) -> None:
    """An ``isom`` MP4 holding only AAC is audio, not video."""
    target = tmp_path / "tone.mp4"
    run_ffmpeg(*AUDIO_IN, "-c:a", "aac", str(target))

    assert_that(detect_media_type(target)).is_equal_to(MediaType.AUDIO)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("notes.txt", None), ("x.jpg", MediaType.IMAGE)],
)
def test_detect_zero_prefixed_text_is_not_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    expected: MediaType | None,
) -> None:
    """Two leading zero bytes never make a file audio."""
    target = tmp_path / name
    target.write_bytes(b"\x00\x00hello, this is plain text\n")
    # Pin the two-byte match so the test proves it cannot decide.
    _pin_puremagic(monkeypatch, _match(b"\x00\x00", ".sndr", "audio/x-sndr"))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(decision(result).source).is_equal_to("extension")
    assert_that([s for s in details(result) if s.startswith(".sndr ")]).is_not_empty()


# --- acceptance: extraction reaches the right reader -------------------------


@needs_ffprobe
@pytest.mark.parametrize(
    "name",
    ["aac.m4a", "alac.m4a", "aac_fragments.mp4", "alac_indexed.mp4"],
)
def test_service_extracts_audio_only_mpeg4_duration(name: str) -> None:
    """Audio-only MPEG-4 reaches the audio reader and gets a duration."""
    metadata = DefaultMetadataService().extract(AUDIO_FIXTURES / name)

    assert_that(metadata.duration_seconds).is_not_none()
    assert_that(metadata.sample_rate).is_not_none()


@needs_ffmpeg
@needs_ffprobe
@pytest.mark.parametrize("codec", ["aac", "alac"])
@pytest.mark.parametrize("suffix", ["m4a", "mp4"])
def test_service_extracts_generated_audio_duration(
    tmp_path: Path,
    codec: str,
    suffix: str,
) -> None:
    """Freshly written AAC and ALAC MPEG-4 audio gets a duration."""
    target = tmp_path / f"tone.{suffix}"
    run_ffmpeg("-f", "lavfi", "-i", "sine=duration=0.5", "-c:a", codec, str(target))

    metadata = DefaultMetadataService().extract(target)

    duration = metadata.duration_seconds
    assert_that(duration).is_not_none()
    assert_that(float(duration or 0)).is_close_to(0.5, 0.05)


# --- rule 5: puremagic is advisory -------------------------------------------


@pytest.mark.parametrize("name", ["thing.bin", "thing", "thing.mp3"])
def test_detect_puremagic_alone_does_not_decide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """A puremagic match the extension does not back is only a signal."""
    target = tmp_path / name
    target.write_bytes(_UNRECOGNISED)
    _pin_puremagic(monkeypatch, _match(b"\x01\x02\x03\x04", ".png", "image/png"))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(result.extension_media_type)
    assert_that(result.media_type).is_not_equal_to(MediaType.IMAGE)
    assert_that(decision(result).source).is_equal_to("extension")
    assert_that(details(result)).contains(".png image/png 0.5 bytes=4")


def test_puremagic_agreeing_with_extension_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A match of at least three bytes that the extension backs decides."""
    target = tmp_path / "thing.png"
    target.write_bytes(_UNRECOGNISED)
    _pin_puremagic(
        monkeypatch,
        _match(b"\x01\x02\x03", ".tta", "audio/x-tta"),
        _match(b"\x01\x02\x03", ".png", "image/png"),
    )

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(decision(result).source).is_equal_to("puremagic")
    assert_that(decision(result).detail).contains(".png image/png agrees")


@pytest.mark.parametrize("byte_match", [b"", b"\x01", b"\x01\x02"])
def test_short_puremagic_match_never_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    byte_match: bytes,
) -> None:
    """A match shorter than three bytes never decides, agreeing or not."""
    target = tmp_path / "thing.png"
    target.write_bytes(_UNRECOGNISED)
    _pin_puremagic(monkeypatch, _match(byte_match, ".png", "image/png"))

    result = detect(target)

    assert_that(decision(result).source).is_equal_to("extension")


def test_puremagic_reads_the_open_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """puremagic is fed from the one open handle, never the path (item 3)."""
    target = tmp_path / "thing.bin"
    target.write_bytes(_UNRECOGNISED)
    seen: list[int] = []

    def _stream(handle: BinaryIO) -> list[puremagic.PureMagicWithConfidence]:
        seen.append(os.fstat(handle.fileno()).st_ino)
        return []

    def _by_name(_: str) -> list[puremagic.PureMagicWithConfidence]:
        raise AssertionError("puremagic reopened the path")

    monkeypatch.setattr(puremagic, "magic_stream", _stream)
    monkeypatch.setattr(puremagic, "magic_file", _by_name)
    monkeypatch.setattr(puremagic, "from_file", _by_name)

    detect(target)

    assert_that(seen).is_equal_to([target.stat().st_ino])


def test_puremagic_failure_is_tolerated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A puremagic error leaves the other rules working."""

    def _raise(_: BinaryIO) -> list[object]:
        raise puremagic.PureError("boom")

    monkeypatch.setattr(puremagic, "magic_stream", _raise)
    target = write_ftyp(tmp_path / "x.bin", "isom", ("isom",), ("vide",))

    assert_that(detect_media_type(target)).is_equal_to(MediaType.VIDEO)


# --- non-media content --------------------------------------------------------


def test_pdf_renamed_jpeg(tmp_path: Path) -> None:
    """A PDF named ``.jpg`` is not an image."""
    target = tmp_path / "scan.jpg"
    target.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF\n")

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("pdf")
    assert_that(result.extension_media_type).is_equal_to(MediaType.IMAGE)
    assert_that(decision(result).detail).is_equal_to("decision=pdf non-media signature")


def test_zip_renamed_mp4(tmp_path: Path) -> None:
    """A ZIP archive named ``.mp4`` is not video."""
    target = tmp_path / "clip.mp4"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("a.txt", "hello")

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("zip")


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("song.mp3", b"Just a shopping list:\n- eggs\n- milk\n"),
        ("photo.png", b"<!DOCTYPE html><html><body><p>hi</p></body></html>\n"),
        ("notes.txt", b"Ghost story, plain words"),
        ("bom.mp4", "﻿UTF-8 text with a BOM, café\n".encode()),
    ],
    ids=["text-mp3", "html-png", "text-txt", "bom-mp4"],
)
def test_plain_text_is_not_media(tmp_path: Path, name: str, data: bytes) -> None:
    """Plain text and HTML are non-media whatever the extension says."""
    target = tmp_path / name
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("text")
    assert_that(decision(result).detail).is_equal_to(
        "decision=text non-media plain text"
    )


def test_unrecognised_binary_uses_the_extension(tmp_path: Path) -> None:
    """Bytes no rule recognises fall back to the name, labelled as such."""
    target = tmp_path / "frame.xwd"
    target.write_bytes(_UNRECOGNISED)

    result = detect(target)

    assert_that(result.container).is_none()
    assert_that(decision(result).detail).is_equal_to("decision=extension fallback")


# --- the other rules and the result model ------------------------------------


def test_detect_extension_match_reports_agreement() -> None:
    """A JPEG named ``.jpg`` agrees with its name."""
    result = detect(FIXTURES / "sample.jpg")

    assert_that(result.agrees_with_extension).is_true()
    assert_that(result.container).is_equal_to("jpeg")


def test_detect_extension_mismatch_reports_disagreement(tmp_path: Path) -> None:
    """A JPEG named ``.mp4`` is an image that disagrees with its name."""
    target = tmp_path / "photo.mp4"
    shutil.copyfile(FIXTURES / "sample.jpg", target)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(result.extension_media_type).is_equal_to(MediaType.VIDEO)
    assert_that(result.agrees_with_extension).is_false()


def test_override_decides_and_still_records_evidence(tmp_path: Path) -> None:
    """A user override decides; content evidence is still listed."""
    registry = create_default_format_registry()
    registry.register(extension="heic", media_type=MediaType.VIDEO)
    target = tmp_path / "photo.heic"
    shutil.copyfile(FIXTURES / "sample.heic", target)

    result = detect(target, registry=registry)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(decision(result).source).is_equal_to("override")
    assert_that([s.source for s in result.signals]).contains("puremagic", "extension")


def test_missing_file_uses_extension_and_says_why(tmp_path: Path) -> None:
    """An unreadable file falls back to its name with an unavailable signal."""
    result = detect(tmp_path / "gone.mp4")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(details(result)).contains("unavailable=content FileNotFoundError")


def test_result_is_frozen() -> None:
    """Results and signals cannot be changed after detection."""
    result = detect(FIXTURES / "sample.heic")

    name = "media_type"
    with pytest.raises(ValueError, match="frozen"):
        setattr(result, name, None)


def test_detect_media_type_still_importable_from_registry() -> None:
    """The public import path and its return type are unchanged."""
    from winnow.media import detect_media_type as package_level
    from winnow.media.registry import detect_media_type as registry_level

    assert_that(registry_level).is_same_as(package_level)
    assert_that(registry_level(FIXTURES / "sample.png")).is_equal_to(MediaType.IMAGE)


def test_read_error_falls_back_to_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O error while reading gives the extension with a signal."""
    target = write_ftyp(tmp_path / "clip.mp4", "isom", ("isom",))

    def _fail(_: object, __: int) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(_detect, "inspect_bmff", _fail)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(details(result)).contains("unavailable=content OSError")


# --- files that are not regular ----------------------------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_detect_does_not_block_on_fifo(tmp_path: Path) -> None:
    """A FIFO is not read; the extension answers and a signal says why."""
    target = tmp_path / "pipe.mp4"
    os.mkfifo(target)

    result = detect_with_deadline(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(decision(result).detail).is_equal_to("decision=extension fallback")
    assert_that(details(result)).contains(_detect.NOT_REGULAR)
    assert_that([s.source for s in result.signals]).does_not_contain("puremagic")


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="no /dev/null")
def test_detect_does_not_read_a_device() -> None:
    """A character device is not read."""
    result = detect_with_deadline(Path("/dev/null"))

    assert_that(result.media_type).is_none()
    assert_that(details(result)).contains(_detect.NOT_REGULAR)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix sockets")
def test_detect_does_not_open_a_socket() -> None:
    """A Unix socket is not opened."""
    # A short directory: socket paths are limited to about 100 bytes.
    with tempfile.TemporaryDirectory() as short:
        target = Path(short) / "s.mp3"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(target))
            result = detect_with_deadline(target)
        finally:
            server.close()

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(details(result)).contains(_detect.NOT_REGULAR)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_detect_override_on_fifo_still_reports_unread_content(
    tmp_path: Path,
) -> None:
    """A user override decides for a FIFO without reading it."""
    registry = create_default_format_registry()
    registry.register(extension="pipe", media_type=MediaType.AUDIO)
    target = tmp_path / "x.pipe"
    os.mkfifo(target)

    result = detect(target, registry=registry)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(decision(result).source).is_equal_to("override")
    assert_that(details(result)).contains(_detect.NOT_REGULAR)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_detect_catches_file_swapped_for_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real FIFO swapped in after the check is opened without blocking.

    The swap happens between the path check and the open (carried item 4).
    The open must be non-blocking: a FIFO with no writer would otherwise
    block it, and the deadline fails the test.
    """
    target = tmp_path / "clip.mp4"
    target.write_bytes(
        write_ftyp(tmp_path / "real.mp4", "isom", ("isom",)).read_bytes(),
    )
    real_is_regular = stat.S_ISREG
    swapped: list[bool] = []

    def _check_then_swap(mode: int) -> bool:
        answer = real_is_regular(mode)
        if not swapped:
            swapped.append(True)
            target.unlink()
            os.mkfifo(target)
        return answer

    monkeypatch.setattr(stat, "S_ISREG", _check_then_swap)

    result = detect_with_deadline(target)

    assert_that(swapped).is_equal_to([True])
    assert_that(stat.S_IFMT(target.lstat().st_mode)).is_equal_to(stat.S_IFIFO)
    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(details(result)).contains(_detect.NOT_REGULAR)


def test_ffmpeg_helper_skips_without_encoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing encoder skips the test instead of failing it."""
    # The helper stops before running anything, so this holds without ffmpeg.
    monkeypatch.setattr(detect_support, "ffmpeg_encoders", lambda: frozenset())

    with pytest.raises(pytest.skip.Exception, match="libvpx"):
        run_ffmpeg(
            *VIDEO_IN,
            "-c:v",
            "libvpx",
            str(tmp_path / "x.webm"),
            binary=sys.executable,
        )
