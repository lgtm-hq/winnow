"""Tests for content-based media-type detection (#393, PR A).

Header-only cases are tiny literal box structures and assert detection only.
Cases that extract metadata use complete media: committed fixtures or files
written at test time by Pillow, pillow-heif, or ffmpeg (skipped without it).
"""

from __future__ import annotations

import functools
import itertools
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import puremagic
import pytest
from assertpy import assert_that
from PIL import Image

from tests.media.bmff_support import (
    box,
    canon_uuid,
    ftyp,
    large_box,
    moov,
    track,
    write_ftyp,
)
from winnow.media import _detect
from winnow.media import service as service_module
from winnow.media._detect import TRACK_BOX, DetectionResult, TypeSignal, detect
from winnow.media._ffprobe import FFMPEG as FFMPEG_NAME
from winnow.media._ffprobe import FFPROBE as FFPROBE_NAME
from winnow.media._ffprobe import find_binary, run_command
from winnow.media.registry import (
    RAW_IMAGE_MIME_TYPES,
    create_default_format_registry,
    detect_media_type,
)
from winnow.media.service import DefaultMetadataService
from winnow.models.media import MediaMetadata, MediaType

FIXTURES = Path(__file__).resolve().parent / "fixtures"
AUDIO_FIXTURES = FIXTURES / "audio"
FFMPEG = find_binary(FFMPEG_NAME)
FFPROBE = find_binary(FFPROBE_NAME)
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
needs_ffprobe = pytest.mark.skipif(FFPROBE is None, reason="ffprobe not installed")

LIBHEIF_BRANDS = ("mif1", "heic", "miaf")
CAMERA_BRANDS = ("mif1", "MiHE", "MiPr", "miaf", "MiHB", "heic")
CR3_PROFILE = b"CanonCR3_001/00.09.00/00.00.00"
CRM_PROFILE = b"CanonCRM0001/02.09.00/00.00.00"


_CODEC_FLAGS = frozenset({"-c", "-c:a", "-c:v", "-codec", "-acodec", "-vcodec"})


@functools.cache
def _ffmpeg_encoders() -> frozenset[str]:
    """Return the encoders the installed ffmpeg offers.

    Returns:
        Encoder names; empty without ffmpeg or when it cannot list them.
    """
    if FFMPEG is None:
        return frozenset()
    completed = run_command(
        argv=[FFMPEG, "-hide_banner", "-encoders"],
        timeout=60,
        operation="list ffmpeg encoders",
        path=Path(FFMPEG),
    )
    names = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        # Rows look like " V....D libx264  description"; the legend ends at "------".
        if len(fields) >= 2 and len(fields[0]) == 6 and fields[0][0] in {"V", "A", "S"}:
            names.add(fields[1])
    return frozenset(names)


def _ffmpeg(*args: str) -> None:
    """Run ffmpeg quietly with a fixed argument vector.

    Skips the test when ffmpeg, or an encoder ``args`` names, is missing.

    Args:
        args: Arguments after the binary and the quiet flags.
    """
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    for flag, value in itertools.pairwise(args):
        if flag in _CODEC_FLAGS and value not in _ffmpeg_encoders():
            pytest.skip(f"ffmpeg encoder {value} not available")
    completed = run_command(
        argv=[FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
        timeout=60,
        operation="write test sample",
        path=Path(args[-1]),
    )
    assert_that(completed.returncode).described_as(completed.stderr).is_zero()


def _decision(result: DetectionResult) -> TypeSignal:
    """Return the signal that carries the decision.

    Args:
        result: Detection result.

    Returns:
        The single ``decision=`` signal.
    """
    decisions = [s for s in result.signals if s.detail.startswith("decision=")]
    assert_that(decisions).is_length(1)
    return decisions[0]


def _details(result: DetectionResult) -> list[str]:
    """Return every signal detail.

    Args:
        result: Detection result.

    Returns:
        Signal details in order.
    """
    return [signal.detail for signal in result.signals]


def _save_heif(path: Path, image: Image.Image) -> None:
    """Save ``image`` as HEIF with pillow-heif, skipping when it cannot.

    Args:
        path: Destination.
        image: Image to save.
    """
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        image.save(path, format="HEIF")
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        pytest.skip(f"pillow-heif cannot encode HEIF here: {exc}")


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
    target = FIXTURES / "sample.heic"
    real = puremagic.magic_file(str(target))
    video_first = [
        puremagic.main.PureMagicWithConfidence(
            byte_match=b"ftypheic",
            offset=4,
            extension=".mp4",
            mime_type="video/mp4",
            name="MPEG-4 video",
            confidence=0.8,
        ),
        *real,
    ]
    monkeypatch.setattr(puremagic, "magic_file", lambda _: video_first)

    assert_that(detect_media_type(target)).is_equal_to(MediaType.IMAGE)


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
    _ffmpeg("-f", "lavfi", "-i", "sine=duration=0.5", "-c:a", codec, str(written))
    target = written.rename(tmp_path / name)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_details(result)).contains("hdlr=soun", "major_brand=M4A ")


@needs_ffmpeg
def test_detect_audio_only_mp4_is_audio(tmp_path: Path) -> None:
    """An ``isom`` MP4 holding only AAC is audio, not video."""
    target = tmp_path / "tone.mp4"
    _ffmpeg("-f", "lavfi", "-i", "sine=duration=0.5", "-c:a", "aac", str(target))

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
    # Not every puremagic version reports this match; pin it so the test
    # proves the two-byte match cannot decide wherever it does appear.
    two_byte_match = puremagic.main.PureMagicWithConfidence(
        byte_match=b"\x00\x00",
        offset=0,
        extension=".sndr",
        mime_type="audio/x-sndr",
        name="Macintosh SNDR resource",
        confidence=0.2,
    )
    monkeypatch.setattr(puremagic, "magic_file", lambda _: [two_byte_match])

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(_decision(result).source).is_equal_to("extension")
    sndr = [s for s in result.signals if s.detail.startswith(".sndr ")]
    assert_that(sndr).is_not_empty()


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
    _ffmpeg("-f", "lavfi", "-i", "sine=duration=0.5", "-c:a", codec, str(target))

    metadata = DefaultMetadataService().extract(target)

    duration = metadata.duration_seconds
    assert_that(duration).is_not_none()
    assert_that(float(duration or 0)).is_close_to(0.5, 0.05)


# --- HEIF and AVIF brands -----------------------------------------------------


@pytest.mark.parametrize(
    "compatible",
    [LIBHEIF_BRANDS, CAMERA_BRANDS],
    ids=["libheif", "camera"],
)
def test_detect_heic_ftyp_kinds(tmp_path: Path, compatible: Sequence[str]) -> None:
    """Both HEIC ``ftyp`` layouts the issue describes are images."""
    target = tmp_path / "photo.bin"
    target.write_bytes(ftyp("heic", compatible) + box(b"meta", b"\x00" * 12))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(_details(result)).contains(
        "major_brand=heic",
        f"compatible_brands={','.join(compatible)}",
        "brand_family=heif-image brands=heic,miaf,mif1",
        "decision=iso-bmff image brand",
    )


def test_detect_generated_heif(tmp_path: Path) -> None:
    """A HEIF written by pillow-heif at test time is an image."""
    target = tmp_path / "fresh.heic"
    _save_heif(target, Image.new("RGB", (16, 16), (0, 128, 255)))

    assert_that(detect_media_type(target)).is_equal_to(MediaType.IMAGE)


@pytest.mark.parametrize(
    "compatible",
    list(itertools.permutations(CAMERA_BRANDS[:4])),
)
def test_compatible_brand_permutation(
    tmp_path: Path,
    compatible: tuple[str, ...],
) -> None:
    """Reordering compatible brands never changes the answer or its families."""
    target = tmp_path / "photo.bin"
    target.write_bytes(ftyp("heic", compatible))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    families = [d for d in _details(result) if d.startswith("brand_family=")]
    assert_that(families).is_equal_to(["brand_family=heif-image brands=heic,miaf,mif1"])


@pytest.mark.parametrize(
    "order",
    [("soun", "vide"), ("vide", "soun")],
)
def test_compatible_brand_permutation_with_tracks(
    tmp_path: Path,
    order: tuple[str, str],
) -> None:
    """Brand order and track order together leave a movie a video."""
    for compatible in itertools.permutations(("isom", "M4A ", "mp42")):
        target = write_ftyp(tmp_path / "clip.bin", "isom", compatible, order)
        assert_that(detect_media_type(target)).is_equal_to(MediaType.VIDEO)


@pytest.mark.parametrize(
    ("major", "compatible"),
    [
        ("isom", ("isom", "avif")),
        ("mif1", ("mif1", "avif", "miaf")),
        ("avif", ("mif1", "avif", "miaf")),
        ("mif1", ("mif1", "heic")),
        ("isom", ("mif1",)),
    ],
)
def test_generic_major_specific_compatible(
    tmp_path: Path,
    major: str,
    compatible: tuple[str, ...],
) -> None:
    """A specific image brand among the compatible brands decides image."""
    target = write_ftyp(tmp_path / "image.bin", major, compatible, tracks=None)

    assert_that(detect_media_type(target)).is_equal_to(MediaType.IMAGE)


def test_detect_pillow_avif(tmp_path: Path) -> None:
    """An AVIF written by Pillow is an image."""
    target = tmp_path / "pic.avif"
    try:
        Image.new("RGB", (16, 16), (10, 200, 30)).save(target, format="AVIF")
    except (KeyError, OSError) as exc:
        pytest.skip(f"Pillow cannot write AVIF here: {exc}")

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(result.major_brand).is_equal_to("avif")


@pytest.mark.parametrize("brand", ["avis", "msf1", "hevc"])
def test_avis_sequence_is_image(tmp_path: Path, brand: str) -> None:
    """Image sequences stay images although their track handler is ``vide``."""
    target = write_ftyp(tmp_path / "seq.bin", brand, (brand, "iso8"), ("vide",))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(_details(result)).contains(
        f"brand_family=image-sequence brands={brand}",
        "decision=iso-bmff image-sequence brand",
    )


def test_image_and_audio_brands_conflict(tmp_path: Path) -> None:
    """Specific image and audio brands together are unknown."""
    target = write_ftyp(tmp_path / "odd.m4a", "M4A ", ("M4A ", "heic"), ("soun",))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains("conflict")


# --- track handlers -----------------------------------------------------------


@pytest.mark.parametrize("brand", ["M4A ", "M4B ", "M4P ", "F4A ", "F4B "])
def test_m4a_brand_with_video_track(tmp_path: Path, brand: str) -> None:
    """An audio brand is a hint; a ``vide`` track makes the file video."""
    target = write_ftyp(tmp_path / "x.m4a", brand, (brand, "isom"), ("soun", "vide"))

    assert_that(detect_media_type(target)).is_equal_to(MediaType.VIDEO)


@pytest.mark.parametrize("brand", ["M4A ", "M4B ", "F4A "])
def test_audio_brand_with_sound_track_is_audio(tmp_path: Path, brand: str) -> None:
    """An audio brand with only a ``soun`` track is audio."""
    target = write_ftyp(tmp_path / "x.bin", brand, (brand,), ("soun",))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_details(result)).contains(
        f"brand_family=audio-hint brands={brand}",
        "decision=iso-bmff track handlers soun without vide",
    )


def test_audio_handler_before_video_handler(tmp_path: Path) -> None:
    """Audio is decided only after every track; a later ``vide`` wins."""
    target = write_ftyp(tmp_path / "x.mp4", "isom", ("isom",), ("soun", "vide"))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_details(result)).contains("hdlr=soun", "hdlr=vide")


@pytest.mark.parametrize(
    ("brand", "tracks", "expected"),
    [
        ("isom", ("vide",), MediaType.VIDEO),
        ("mp41", ("vide",), MediaType.VIDEO),
        ("mp42", ("soun",), MediaType.AUDIO),
        ("qt  ", ("vide", "soun"), MediaType.VIDEO),
        ("M4V ", ("vide",), MediaType.VIDEO),
        ("3gp4", ("vide",), MediaType.VIDEO),
        ("3g2a", ("vide",), MediaType.VIDEO),
        ("F4V ", ("vide",), MediaType.VIDEO),
        ("zzzz", ("soun",), MediaType.AUDIO),
        ("isom", ("tmcd", "text"), None),
        ("isom", (), None),
    ],
)
def test_non_image_brands_use_track_handlers(
    tmp_path: Path,
    brand: str,
    tracks: tuple[str, ...],
    expected: MediaType | None,
) -> None:
    """Generic, QuickTime, 3GP, F4V and unknown brands follow the handlers."""
    target = write_ftyp(tmp_path / "clip.mp4", brand, (brand,), tracks)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(_details(result)).contains(f"major_brand={brand}")


@pytest.mark.parametrize("brand", ["F4V ", "M4A ", "isom"])
def test_ftyp_without_moov_is_unknown(tmp_path: Path, brand: str) -> None:
    """A non-image ``ftyp`` with no ``moov`` does not fall back to the name."""
    target = write_ftyp(tmp_path / "clip.mp4", brand, (brand,), tracks=None)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.extension_media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_decision(result).detail).contains("no moov")


def test_detect_moov_after_mdat(tmp_path: Path) -> None:
    """``moov`` after a 64 MiB sparse ``mdat`` is found without reading it."""
    target = tmp_path / "late.mp4"
    mdat_size = 64 * 1024 * 1024
    with target.open("wb") as handle:
        handle.write(ftyp("isom", ("isom",)))
        handle.write(struct.pack(">I", mdat_size) + b"mdat")
        handle.seek(mdat_size - 8, 1)
        handle.write(moov(("soun",)))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_details(result)).contains("hdlr=soun")


def test_ftyp_after_free(tmp_path: Path) -> None:
    """``ftyp`` need not be the first box."""
    target = tmp_path / "x.bin"
    target.write_bytes(box(b"free", b"\x00" * 8) + ftyp("heic", ("mif1", "heic")))

    assert_that(detect_media_type(target)).is_equal_to(MediaType.IMAGE)


def test_moov_before_ftyp_is_decided_at_moov(tmp_path: Path) -> None:
    """A ``moov`` read before any ``ftyp`` is legacy QuickTime; the walk stops."""
    target = tmp_path / "x.bin"
    target.write_bytes(moov(("vide",)) + ftyp("M4V ", ("M4V ",)))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(result.container).is_equal_to("quicktime")
    assert_that(result.major_brand).is_none()


@pytest.mark.parametrize(
    "tail",
    [
        struct.pack(">I", 1 << 20) + b"mdat" + b"\x00" * 16,
        b"\x00\x00\x00",
        struct.pack(">I", 3) + b"free",
        ftyp("isom")[:12],
    ],
    ids=["mdat-past-eof", "truncated-header", "size-3", "cut-ftyp"],
)
def test_legacy_quicktime_stops_once_moov_is_read(tmp_path: Path, tail: bytes) -> None:
    """Nothing after a legacy ``moov`` can turn the file unknown (finding 8)."""
    target = tmp_path / "old.mov"
    target.write_bytes(moov(("vide", "soun")) + tail)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(result.container).is_equal_to("quicktime")
    assert_that(_decision(result).detail).is_equal_to(
        "decision=quicktime track handler vide",
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (moov(("vide", "soun")) + box(b"mdat"), MediaType.VIDEO),
        (box(b"mdat", b"\x00" * 32) + moov(("soun",)), MediaType.AUDIO),
        (box(b"wide") + box(b"mdat") + moov(("vide",)), MediaType.VIDEO),
        (moov(()) + box(b"mdat"), None),
        (box(b"mdat", b"\x00" * 32), None),
    ],
    ids=["moov-first", "moov-after-mdat", "wide-mdat-moov", "no-tracks", "no-moov"],
)
def test_legacy_mov_without_ftyp(
    tmp_path: Path,
    data: bytes,
    expected: MediaType | None,
) -> None:
    """Legacy QuickTime without ``ftyp`` is decided by its tracks."""
    target = tmp_path / "old.mov"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_equal_to("quicktime")
    assert_that(result.major_brand).is_none()
    assert_that(_decision(result).detail).starts_with("decision=quicktime")


# --- Canon CRX ----------------------------------------------------------------


def _write_canon(path: Path, profile: bytes | None) -> Path:
    """Write a bounded synthetic Canon CRX file with ``vide`` tracks.

    Args:
        path: Destination.
        profile: ``CNCV`` payload, or ``None`` for no Canon uuid box.

    Returns:
        ``path``.
    """
    extra = b"" if profile is None else canon_uuid(profile)
    data = ftyp("crx ", ("crx ", "isom"))
    data += moov(("vide", "vide", "vide"), extra=extra) + box(b"mdat", b"\x00" * 16)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("name", ["IMG_0001.CR3", "renamed.bin"])
def test_canon_cr3_profile_is_image(tmp_path: Path, name: str) -> None:
    """An established CR3 profile is an image despite its ``vide`` tracks."""
    result = detect(_write_canon(tmp_path / name, CR3_PROFILE))

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(_details(result)).contains(
        "hdlr=vide",
        "canon_cncv=CanonCR3_001/00.09.00/00.00.00",
        "decision=iso-bmff canon CR3 profile",
    )


@pytest.mark.parametrize("name", ["MVI_0001.CRM", "renamed.cr3", "renamed.bin"])
def test_canon_crm_profile_is_video(tmp_path: Path, name: str) -> None:
    """An established CRM profile is video, even named ``.cr3``."""
    result = detect(_write_canon(tmp_path / name, CRM_PROFILE))

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_decision(result).detail).contains("canon CRM profile")


@pytest.mark.parametrize("name", ["IMG_0001.cr3", "renamed.bin"])
def test_canon_crx_without_profile_is_unknown(tmp_path: Path, name: str) -> None:
    """A bare ``crx`` brand lacks evidence and never falls back to ``.cr3``."""
    result = detect(_write_canon(tmp_path / name, None))

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains("insufficient-evidence")
    assert_that(_details(result)).contains("brand_family=canon-crx brands=crx ")


def test_canon_crx_ftyp_only_is_unknown(tmp_path: Path) -> None:
    """A ``crx`` ``ftyp`` with nothing after it is unknown."""
    target = tmp_path / "x.cr3"
    target.write_bytes(ftyp("crx ", ("crx ", "isom")))

    assert_that(detect_media_type(target)).is_none()


def test_canon_unsupported_profile_is_unknown(tmp_path: Path) -> None:
    """A Canon profile that is neither CR3 nor CRM is unknown."""
    result = detect(_write_canon(tmp_path / "x.cr3", b"CanonXYZ/1"))

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains("unsupported")


def test_canon_uuid_with_other_usertype_is_ignored(tmp_path: Path) -> None:
    """Only the documented Canon usertype is searched for ``CNCV``."""
    other = box(b"uuid", b"\x11" * 16 + box(b"CNCV", CR3_PROFILE))
    data = ftyp("crx ", ("crx ",)) + moov(("vide",), extra=other)
    target = tmp_path / "x.cr3"
    target.write_bytes(data)

    assert_that(detect_media_type(target)).is_none()


# --- bounds and malformed structures ----------------------------------------


def test_detect_truncated_ftyp_falls_through(tmp_path: Path) -> None:
    """Fewer bytes than one box header is not ISO-BMFF; the name decides."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftyp"[:6])

    result = detect(target)

    assert_that(result.container).is_none()
    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_decision(result).source).is_equal_to("extension")


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (ftyp("isom", ("isom",))[:18], "box extends past the file"),
        (struct.pack(">I", 0xFFFFFFFF) + b"ftypisom", "box extends past the file"),
        (box(b"ftyp"), "ftyp shorter than its minimum size"),
        (box(b"ftyp", b"isom\x00\x00\x00\x00is"), "not a multiple of four"),
        (struct.pack(">I", 4) + b"ftypisom\x00\x00\x00\x00", "non-progressing"),
        (ftyp("isom") + struct.pack(">I", 3) + b"free", "non-progressing"),
        (ftyp("isom") + b"\x00\x00\x00", "truncated box header"),
        (ftyp("isom") + struct.pack(">I", 1) + b"mdat\x00\x00", "truncated extended"),
        (
            ftyp("isom") + struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 8),
            "non-progressing",
        ),
        (
            ftyp("isom") + struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 2**63),
            "box extends past the file",
        ),
        (
            ftyp("isom")
            + box(b"moov", struct.pack(">I", 64) + TRACK_BOX)
            + box(b"mdat", b"\x00" * 64),
            "box extends past its parent",
        ),
        (
            ftyp("isom") + box(b"moov", box(TRACK_BOX, box(b"mdia", box(b"hdlr")))),
            "hdlr shorter",
        ),
        (ftyp("isom") + box(b"uuid", b"\x00" * 8), "uuid box shorter"),
    ],
    ids=[
        "ftyp-past-eof",
        "size-0xffffffff",
        "size-8-ftyp",
        "ftyp-partial-brand",
        "size-4",
        "size-3-later",
        "truncated-header",
        "truncated-largesize",
        "largesize-below-header",
        "largesize-huge",
        "child-past-parent",
        "short-hdlr",
        "short-uuid",
    ],
)
def test_detect_bad_box_size_is_bounded(
    tmp_path: Path,
    data: bytes,
    reason: str,
) -> None:
    """Malformed boxes give unknown with the reason, never the extension."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.extension_media_type).is_equal_to(MediaType.VIDEO)
    assert_that(result.agrees_with_extension).is_none()
    assert_that(_decision(result).detail).contains("malformed").contains(reason)


def test_detect_nesting_loop_hits_depth_cap(tmp_path: Path) -> None:
    """Containers nested past the depth cap are over budget, not followed."""
    nested = box(TRACK_BOX, box(b"mdia", b""))
    for _ in range(_detect.MAX_BOX_DEPTH + 2):
        nested = box(b"moov", nested)
    target = tmp_path / "deep.mp4"
    target.write_bytes(ftyp("isom", ("isom",)) + nested)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains("budget-exceeded: box depth")


def test_detect_box_count_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More boxes than the cap are over budget."""
    monkeypatch.setattr(_detect, "MAX_BOXES", 16)
    target = tmp_path / "many.mp4"
    target.write_bytes(
        ftyp("isom", ("isom",)) + box(b"free") * 32 + moov(("soun",)),
    )

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains("budget-exceeded: box count")


def test_detect_read_budget_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading more than the byte budget is over budget."""
    monkeypatch.setattr(_detect, "MAX_BYTES_READ", 64)
    target = tmp_path / "many.mp4"
    target.write_bytes(ftyp("isom", ("isom",)) + box(b"free") * 16 + moov(("soun",)))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains("budget-exceeded: read budget")


def test_large_ftyp_is_over_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid ``ftyp`` larger than its read budget is not malformed."""
    monkeypatch.setattr(_detect, "MAX_FTYP_PAYLOAD_BYTES", 16)
    target = tmp_path / "x.heic"
    target.write_bytes(ftyp("heic", ("mif1", "heic", "miaf")))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains(
        "budget-exceeded: ftyp larger than its read budget",
    )


@pytest.mark.parametrize("compatible", [(), ("mif1", "heic")])
def test_extended_ftyp_minimum_size(
    tmp_path: Path,
    compatible: tuple[str, ...],
) -> None:
    """A 64-bit ``ftyp`` of 24 bytes and up is complete."""
    payload = b"heic\x00\x00\x00\x00" + "".join(compatible).encode()
    target = tmp_path / "x.bin"
    target.write_bytes(large_box(b"ftyp", payload))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(result.compatible_brands).is_equal_to(compatible)


def test_extended_ftyp_below_minimum_is_malformed(tmp_path: Path) -> None:
    """A 64-bit ``ftyp`` shorter than 24 bytes is malformed."""
    target = tmp_path / "x.bin"
    target.write_bytes(large_box(b"ftyp", b"heic"))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).detail).contains("ftyp shorter")


def test_size_zero_box_extends_to_end(tmp_path: Path) -> None:
    """A size-0 ``mdat`` before ``moov`` runs to the end and swallows it."""
    target = tmp_path / "x.mp4"
    target.write_bytes(
        ftyp("isom", ("isom",)) + b"\x00\x00\x00\x00mdat" + b"x" * 40 + moov(("soun",)),
    )

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(_decision(result).detail).is_equal_to(
        "decision=iso-bmff unknown unresolved: no moov",
    )


@pytest.mark.parametrize(
    ("data", "expected", "reason"),
    [
        (
            box(b"wide") + b"\x00\x00\x00\x00mdat" + b"x" * 24,
            None,
            "decision=quicktime unknown unresolved: no moov",
        ),
        (
            box(
                b"moov",
                box(b"mvhd", b"\x00" * 16)
                + track("vide")
                + b"\x00\x00\x00\x00udta"
                + b"\x00" * 12,
            ),
            MediaType.VIDEO,
            "decision=quicktime track handler vide",
        ),
    ],
    ids=["top-level-mdat", "last-child-of-moov"],
)
def test_legacy_quicktime_steps_past_size_zero_box(
    tmp_path: Path,
    data: bytes,
    expected: MediaType | None,
    reason: str,
) -> None:
    """The walker steps past a size-0 box to its boundary without error."""
    target = tmp_path / "old.mov"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_equal_to("quicktime")
    assert_that(_decision(result).detail).is_equal_to(reason)


def test_size_zero_moov_extends_to_end(tmp_path: Path) -> None:
    """A size-0 ``moov`` is walked to the end of the file."""
    target = tmp_path / "x.mp4"
    body = moov(("vide",))[8:]
    target.write_bytes(ftyp("isom", ("isom",)) + b"\x00\x00\x00\x00moov" + body)

    assert_that(detect_media_type(target)).is_equal_to(MediaType.VIDEO)


# --- the other rules and the result model ------------------------------------


def test_detect_extension_match_reports_agreement() -> None:
    """A JPEG named ``.jpg`` agrees with its name."""
    result = detect(FIXTURES / "sample.jpg")

    assert_that(result.agrees_with_extension).is_true()


def test_detect_extension_mismatch_reports_disagreement(tmp_path: Path) -> None:
    """A JPEG named ``.mp4`` is an image that disagrees with its name."""
    target = tmp_path / "photo.mp4"
    shutil.copyfile(FIXTURES / "sample.jpg", target)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(result.extension_media_type).is_equal_to(MediaType.VIDEO)
    assert_that(result.agrees_with_extension).is_false()


def _sndr_match(byte_match: bytes = b"\x00\x00") -> puremagic.PureMagicWithConfidence:
    """Build puremagic's ``.sndr`` match.

    Args:
        byte_match: The bytes the match compared.

    Returns:
        The match.
    """
    return puremagic.main.PureMagicWithConfidence(
        byte_match=byte_match,
        offset=0,
        extension=".sndr",
        mime_type="audio/x-sndr",
        name="Macintosh SNDR resource",
        confidence=0.2,
    )


@pytest.mark.parametrize("name", ["sound.unknown-ext", "sound.bin", "sound"])
def test_unclaimed_file_takes_first_puremagic_media_match(
    tmp_path: Path,
    name: str,
) -> None:
    """Outside rule 2, the first puremagic media match decides, as on ``main``.

    The extension plays no part, so the host's MIME table cannot change it.
    """
    target = tmp_path / name
    shutil.copyfile(AUDIO_FIXTURES / "tta.tta", target)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(result.container).is_none()
    assert_that(_decision(result).source).is_equal_to("puremagic")
    assert_that(_decision(result).detail).contains(".tta")


@pytest.mark.parametrize("byte_match", [b"", b"\x00", b"\x00\x00"])
def test_short_sndr_match_never_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    byte_match: bytes,
) -> None:
    """A ``audio/x-sndr`` match under three bytes is skipped for the next one."""
    target = tmp_path / "noise.bin"
    target.write_bytes(b"\x00\x00not really anything")
    image = puremagic.main.PureMagicWithConfidence(
        byte_match=b"\x89PNG",
        offset=0,
        extension=".png",
        mime_type="image/png",
        name="PNG",
        confidence=0.1,
    )
    monkeypatch.setattr(
        puremagic,
        "magic_file",
        lambda _: [_sndr_match(byte_match), image],
    )

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(_decision(result).detail).contains(".png image/png")


def test_long_sndr_match_still_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the short ``audio/x-sndr`` match is excluded."""
    target = tmp_path / "noise.bin"
    target.write_bytes(b"\x00\x00\x00not really anything")
    monkeypatch.setattr(
        puremagic,
        "magic_file",
        lambda _: [_sndr_match(b"\x00\x00\x00")],
    )

    assert_that(detect_media_type(target)).is_equal_to(MediaType.AUDIO)


def test_short_match_of_another_mime_type_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A two-byte match that is not ``audio/x-sndr`` decides, as on ``main``."""
    target = tmp_path / "noise.txt"
    target.write_bytes(b"\x0b\x77not really ac3")
    monkeypatch.setattr(
        puremagic,
        "magic_file",
        lambda _: [
            puremagic.main.PureMagicWithConfidence(
                byte_match=b"\x0b\x77",
                offset=0,
                extension=".ac3",
                mime_type="audio/ac3",
                name="AC-3",
                confidence=0.2,
            ),
        ],
    )

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_decision(result).source).is_equal_to("puremagic")


def test_puremagic_failure_is_tolerated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A puremagic error leaves the other rules working."""

    def _raise(_: str) -> list[object]:
        raise puremagic.PureError("boom")

    monkeypatch.setattr(puremagic, "magic_file", _raise)
    target = write_ftyp(tmp_path / "x.bin", "isom", ("isom",), ("vide",))

    assert_that(detect_media_type(target)).is_equal_to(MediaType.VIDEO)


def test_text_without_media_match_uses_name(tmp_path: Path) -> None:
    """A file puremagic cannot type as media defers to its name."""
    target = tmp_path / "notes.txt"
    target.write_bytes(b"Ghost story, plain words")

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(_decision(result).source).is_equal_to("extension")


@pytest.mark.parametrize(
    ("name", "expected"),
    [("sample.jpg", MediaType.IMAGE), ("sample.mp3", MediaType.AUDIO)],
)
def test_signature_formats_keep_main_behaviour(
    name: str,
    expected: MediaType,
) -> None:
    """Formats outside rule 2 still detect through puremagic until PR B."""
    result = detect(FIXTURES / name)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(_decision(result).source).is_equal_to("puremagic")
    assert_that(_decision(result).detail).contains("rules 3-5 pending")


def test_frame_sync_mp3_keeps_main_behaviour(tmp_path: Path) -> None:
    """An MP3 without ID3 renamed ``.bin`` is still audio, as on ``main``."""
    target = tmp_path / "frames.bin"
    shutil.copyfile(AUDIO_FIXTURES / "vbr_noxing.mp3", target)

    assert_that(detect_media_type(target)).is_equal_to(MediaType.AUDIO)


def test_override_decides_and_still_records_evidence(tmp_path: Path) -> None:
    """A user override decides; content evidence is still listed."""
    registry = create_default_format_registry()
    registry.register(extension="heic", media_type=MediaType.VIDEO)
    target = tmp_path / "photo.heic"
    shutil.copyfile(FIXTURES / "sample.heic", target)

    result = detect(target, registry=registry)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_decision(result).source).is_equal_to("override")
    assert_that([s.source for s in result.signals]).contains("puremagic", "extension")


def test_missing_file_uses_extension_and_says_why(tmp_path: Path) -> None:
    """An unreadable file falls back to its name with an unavailable signal."""
    result = detect(tmp_path / "gone.mp4")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_details(result)).contains("unavailable=content FileNotFoundError")


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


# --- review of PR 449, round 2 ----------------------------------------------


def _detect_with_deadline(target: Path) -> DetectionResult:
    """Run :func:`detect` in a thread and fail if it blocks.

    Args:
        target: Path to classify.

    Returns:
        The result.
    """
    results: list[DetectionResult] = []
    worker = threading.Thread(target=lambda: results.append(detect(target)))
    worker.daemon = True
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        # Release a reader stuck opening a FIFO, then fail.
        writer = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        worker.join(timeout=5)
        pytest.fail(f"detect blocked on {target}")
    return results[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_detect_does_not_block_on_fifo(tmp_path: Path) -> None:
    """A FIFO is not read; the extension answers and a signal says why."""
    target = tmp_path / "pipe.mp4"
    os.mkfifo(target)

    result = _detect_with_deadline(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_decision(result).detail).is_equal_to("decision=extension fallback")
    assert_that(_details(result)).contains(_detect._NOT_REGULAR)
    assert_that([s.source for s in result.signals]).does_not_contain("puremagic")


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="no /dev/null")
def test_detect_does_not_read_a_device() -> None:
    """A character device is not read."""
    result = _detect_with_deadline(Path("/dev/null"))

    assert_that(result.media_type).is_none()
    assert_that(_details(result)).contains(_detect._NOT_REGULAR)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix sockets")
def test_detect_does_not_open_a_socket() -> None:
    """A Unix socket is not opened."""
    # A short directory: socket paths are limited to about 100 bytes.
    with tempfile.TemporaryDirectory() as short:
        target = Path(short) / "s.mp3"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(target))
            result = _detect_with_deadline(target)
        finally:
            server.close()

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_details(result)).contains(_detect._NOT_REGULAR)


def test_detect_override_on_fifo_still_reports_unread_content(
    tmp_path: Path,
) -> None:
    """A user override decides for a FIFO without reading it."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    registry = create_default_format_registry()
    registry.register(extension="pipe", media_type=MediaType.AUDIO)
    target = tmp_path / "x.pipe"
    os.mkfifo(target)

    result = detect(target, registry=registry)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_decision(result).source).is_equal_to("override")
    assert_that(_details(result)).contains(_detect._NOT_REGULAR)


def test_detect_catches_file_swapped_for_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path that stops being a regular file after the check is not read."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(
        write_ftyp(tmp_path / "real.mp4", "isom", ("isom",)).read_bytes()
    )
    answers = iter([True, False])
    monkeypatch.setattr(stat, "S_ISREG", lambda _: next(answers))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_details(result)).contains(_detect._NOT_REGULAR)


def test_read_error_falls_back_to_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O error while reading gives the extension with a signal."""
    target = write_ftyp(tmp_path / "clip.mp4", "isom", ("isom",))

    def _fail(_: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(_detect, "_inspect_bmff", _fail)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(_details(result)).contains("unavailable=content OSError")


_NON_BMFF_AUDIO = (
    "ac3.ac3",
    "ac3_mono.ac3",
    "eac3.eac3",
    "tta.tta",
    "wavpack.wv",
    "core_audio.bin",
)


@pytest.mark.parametrize("name", _NON_BMFF_AUDIO)
def test_non_bmff_audio_detects_as_on_main(name: str) -> None:
    """Audio outside ISO-BMFF is audio again, as on ``main`` (finding 2)."""
    result = detect(AUDIO_FIXTURES / name)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(result.container).is_none()
    assert_that(_decision(result).source).is_equal_to("puremagic")


@pytest.mark.parametrize("name", _NON_BMFF_AUDIO)
def test_service_dispatches_non_bmff_audio(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """The service sends audio outside ISO-BMFF to the audio reader."""
    dispatched: list[MediaType] = []

    def _recorder(media_type: MediaType) -> Callable[[Path], MediaMetadata]:
        def record(_: Path) -> MediaMetadata:
            dispatched.append(media_type)
            return MediaMetadata()

        return record

    monkeypatch.setattr(
        service_module,
        "_EXTRACTORS",
        {media_type: _recorder(media_type) for media_type in MediaType},
    )
    DefaultMetadataService().extract(AUDIO_FIXTURES / name)

    assert_that(dispatched).is_equal_to([MediaType.AUDIO])


@needs_ffprobe
@pytest.mark.parametrize("name", _NON_BMFF_AUDIO[:-1])
def test_service_extracts_non_bmff_audio(name: str) -> None:
    """Audio outside ISO-BMFF gets its duration again, as on ``main``.

    ``core_audio.bin`` is left out: on ``main`` too every fact is unknown.
    """
    metadata = DefaultMetadataService().extract(AUDIO_FIXTURES / name)

    assert_that(metadata.duration_seconds).is_not_none()
    assert_that(metadata.sample_rate).is_not_none()


def test_single_byte_ts_sync_no_longer_special(tmp_path: Path) -> None:
    """A text file opening with ``0b 77 00 00 47`` detects as on ``main``.

    ``main`` takes puremagic's first media match, the two-byte AC-3 sync
    ``0b 77``; ruling item 1 excludes only short ``audio/x-sndr`` matches.
    The PR's one-byte ``47`` prefix no longer plays any part.
    """
    target = tmp_path / "notes.txt"
    target.write_bytes(b"\x0b\x77\x00\x00Ghello world")

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_decision(result).detail).contains(".ac3 audio/ac3")
    assert_that(_decision(result).detail).does_not_contain("signature prefix")


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("image.ppm", b"P3\n#free\n1 1\n255\n255 0 0\n"),
        ("image.bin", b"P3\n#free\n1 1\n255\n255 0 0\n"),
        (
            "image.svg",
            b"<!--free--><svg xmlns='http://www.w3.org/2000/svg' width='1'"
            b" height='1'/>",
        ),
        ("notes.txt", b"ab: moov is a word, not a box\n"),
        ("notes.txt", b"\x00\x00\x00\x07mdat and seven is too small"),
        ("notes.txt", b"\x00\x00\x00\x01wide\x00\x00\x00\x00\x00\x00\x00\x08x"),
        ("notes.txt", b"\x00\x00\x00\x01skip\x00\x00\x00"),
    ],
    ids=[
        "ppm",
        "ppm-bin",
        "svg",
        "moov-huge",
        "mdat-size-7",
        "wide-large-8",
        "skip-large-cut",
    ],
)
def test_text_that_looks_like_a_box_is_not_claimed(
    tmp_path: Path,
    name: str,
    data: bytes,
) -> None:
    """Only a plausible box size claims a non-``ftyp`` first box (finding 3)."""
    target = tmp_path / name
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.container).is_none()
    sources = {s.source for s in result.signals}
    assert_that(sources).does_not_contain("ftyp", "track-handler")


@pytest.mark.parametrize(
    "data",
    [
        b"\x00\x00\x00\x00free" + b"\x00" * 8,
        box(b"skip", b"\x00" * 8),
        large_box(b"pnot", b"\x00" * 8),
        box(b"moov"),
    ],
    ids=["size-0", "size-in-range", "size-1-large", "size-8"],
)
def test_plausible_first_box_is_claimed(tmp_path: Path, data: bytes) -> None:
    """Plausible sizes of each kind claim the file for rule 2."""
    target = tmp_path / "x.txt"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.container).is_equal_to("quicktime")
    assert_that(result.media_type).is_none()


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (box(b"ftyp"), "malformed: ftyp shorter than its minimum size"),
        (
            ftyp("isom", ["isom"] * 1025),
            "budget-exceeded: ftyp larger than its read budget",
        ),
        (
            struct.pack(">I", 4) + b"ftypisom\x00\x00\x00\x00",
            "malformed: non-progressing",
        ),
    ],
    ids=["8-byte-ftyp", "1025-brands", "size-4-ftyp"],
)
def test_bad_ftyp_is_labelled_iso_bmff(
    tmp_path: Path,
    data: bytes,
    reason: str,
) -> None:
    """An ``ftyp`` seen but not read still names the container (finding 4)."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(_decision(result).source).is_equal_to("ftyp")
    assert_that(_decision(result).detail).starts_with("decision=iso-bmff unknown")
    assert_that(_decision(result).detail).contains(reason)
    assert_that(_details(result)).contains("ftyp=unread")


def test_nested_moov_track_is_not_a_track(tmp_path: Path) -> None:
    """A track box inside a nested ``moov`` is not a track (finding 5)."""
    nested = box(b"moov", track("vide"))
    data = ftyp("isom", ("isom",)) + box(
        b"moov",
        box(b"mvhd", b"\x00" * 16) + nested + track("soun"),
    )
    target = tmp_path / "x.mp4"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(_details(result)).contains("hdlr=soun").does_not_contain("hdlr=vide")


def _tight_file() -> bytes:
    """Build a file rule 2 must read whole: every byte is a header or brand.

    Returns:
        ``ftyp`` and a ``moov`` with one sound track and nothing to skip.
    """
    handler = box(b"hdlr", b"\x00" * 8 + b"soun")
    return ftyp("isom") + box(b"moov", box(TRACK_BOX, box(b"mdia", handler)))


def test_read_budget_counts_the_first_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opening bytes count against the read budget (finding 6)."""
    data = _tight_file()
    target = tmp_path / "x.mp4"
    target.write_bytes(data)
    monkeypatch.setattr(_detect, "MAX_BYTES_READ", len(data))

    over = detect(target)
    monkeypatch.setattr(_detect, "MAX_BYTES_READ", len(data) + _detect._HEAD_BYTES)
    within = detect(target)

    assert_that(over.media_type).is_none()
    assert_that(_decision(over).detail).contains("budget-exceeded: read budget")
    assert_that(within.media_type).is_equal_to(MediaType.AUDIO)


def test_ffmpeg_helper_skips_without_encoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing encoder skips the test instead of failing it (finding 9)."""
    module = sys.modules[__name__]
    # The helper stops before running anything, so this holds without ffmpeg.
    monkeypatch.setattr(module, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(module, "_ffmpeg_encoders", lambda: frozenset())

    with pytest.raises(pytest.skip.Exception, match="libvpx"):
        _ffmpeg(*_VIDEO_IN, "-c:v", "libvpx", str(tmp_path / "x.webm"))


# --- every registry format ----------------------------------------------------


@dataclass(frozen=True)
class _Sample:
    """How to write one sample and what it must detect as.

    Args:
        extension: File extension.
        expected: Expected media type.
        write: Writes the sample to the given path.
        needs: Tool the writer needs (``"ffmpeg"``, ``"heif"`` or ``""``).
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
    _save_heif(path, Image.new("RGB", (8, 6), (1, 2, 3)))


_VIDEO_IN = ("-f", "lavfi", "-i", "testsrc=duration=0.2:size=176x144:rate=10")
_AUDIO_IN = ("-f", "lavfi", "-i", "sine=duration=0.3")


def _ff(*args: str) -> Callable[[Path], None]:
    """Return a writer that runs ffmpeg with ``args`` then the path.

    Args:
        args: Inputs, codecs and format options.

    Returns:
        The writer.
    """

    def write(path: Path) -> None:
        _ffmpeg(*args, str(path))

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
    _write_canon(path, CR3_PROFILE)


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
    _Sample("mp4", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample("mov", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample("m4v", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample("3gp", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "h263"), "ffmpeg"),
    _Sample("3g2", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "h263"), "ffmpeg"),
    _Sample("mkv", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample("webm", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "libvpx"), "ffmpeg"),
    _Sample("avi", MediaType.VIDEO, _ff(*_VIDEO_IN, "-c:v", "mpeg4"), "ffmpeg"),
    _Sample(
        "mts",
        MediaType.VIDEO,
        _ff(*_VIDEO_IN, "-c:v", "mpeg4", "-f", "mpegts"),
        "ffmpeg",
    ),
    _Sample(
        "m2ts",
        MediaType.VIDEO,
        _ff(*_VIDEO_IN, "-c:v", "mpeg4", "-f", "mpegts", "-mpegts_m2ts_mode", "1"),
        "ffmpeg",
    ),
    _Sample("mp3", MediaType.AUDIO, _ff(*_AUDIO_IN, "-c:a", "libmp3lame"), "ffmpeg"),
    _Sample("m4a", MediaType.AUDIO, _ff(*_AUDIO_IN, "-c:a", "aac"), "ffmpeg"),
    _Sample(
        "m4b",
        MediaType.AUDIO,
        _ff(*_AUDIO_IN, "-c:a", "aac", "-f", "ipod"),
        "ffmpeg",
    ),
    _Sample("flac", MediaType.AUDIO, _ff(*_AUDIO_IN, "-c:a", "flac"), "ffmpeg"),
    _Sample("wav", MediaType.AUDIO, _ff(*_AUDIO_IN, "-c:a", "pcm_s16le"), "ffmpeg"),
    _Sample("aiff", MediaType.AUDIO, _ff(*_AUDIO_IN, "-c:a", "pcm_s16be"), "ffmpeg"),
    _Sample(
        "ogg",
        MediaType.AUDIO,
        _ff(*_AUDIO_IN, "-c:a", "libopus", "-f", "ogg"),
        "ffmpeg",
    ),
    _Sample("opus", MediaType.AUDIO, _ff(*_AUDIO_IN, "-c:a", "libopus"), "ffmpeg"),
    _Sample(
        "aac",
        MediaType.AUDIO,
        _ff(*_AUDIO_IN, "-c:a", "aac", "-f", "adts"),
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
    if sample.needs == "ffmpeg" and FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    target = tmp_path / f"sample.{sample.extension}"
    sample.write(target)

    assert_that(detect_media_type(target)).is_equal_to(sample.expected)

    dispatched: list[MediaType] = []

    def _recorder(media_type: MediaType) -> Callable[[Path], MediaMetadata]:
        def record(_: Path) -> MediaMetadata:
            dispatched.append(media_type)
            return MediaMetadata()

        return record

    monkeypatch.setattr(
        service_module,
        "_EXTRACTORS",
        {media_type: _recorder(media_type) for media_type in MediaType},
    )
    DefaultMetadataService().extract(target)

    assert_that(dispatched).is_equal_to([sample.expected])


def test_registry_rows_cover_raw_supplement() -> None:
    """The registry test covers every RAW extension the registry claims."""
    covered = {sample.extension for sample in _RAW}

    assert_that(covered).is_equal_to(set(RAW_IMAGE_MIME_TYPES))
