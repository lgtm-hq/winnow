"""Tests for rule 2 of media-type detection: ISO-BMFF and QuickTime (#393).

Header-only cases are tiny literal box structures and assert detection only.
"""

from __future__ import annotations

import itertools
import struct
from collections.abc import Sequence
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from tests.media.bmff_support import box, canon_uuid, ftyp, moov, write_ftyp
from tests.media.detect_support import (
    CAMERA_BRANDS,
    CR3_PROFILE,
    CRM_PROFILE,
    LIBHEIF_BRANDS,
    decision,
    details,
    save_heif,
    write_canon,
)
from winnow.media._detect import detect
from winnow.media.registry import detect_media_type
from winnow.models.media import MediaType

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
    assert_that(details(result)).contains(
        "major_brand=heic",
        f"compatible_brands={','.join(compatible)}",
        "brand_family=heif-image brands=heic,miaf,mif1",
        "decision=iso-bmff image brand",
    )


def test_detect_generated_heif(tmp_path: Path) -> None:
    """A HEIF written by pillow-heif at test time is an image."""
    target = tmp_path / "fresh.heic"
    save_heif(target, Image.new("RGB", (16, 16), (0, 128, 255)))

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
    families = [d for d in details(result) if d.startswith("brand_family=")]
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
    assert_that(details(result)).contains(
        f"brand_family=image-sequence brands={brand}",
        "decision=iso-bmff image-sequence brand",
    )


def test_image_and_audio_brands_conflict(tmp_path: Path) -> None:
    """Specific image and audio brands together are unknown."""
    target = write_ftyp(tmp_path / "odd.m4a", "M4A ", ("M4A ", "heic"), ("soun",))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains("conflict")


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
    assert_that(details(result)).contains(
        f"brand_family=audio-hint brands={brand}",
        "decision=iso-bmff track handlers soun without vide",
    )


def test_audio_handler_before_video_handler(tmp_path: Path) -> None:
    """Audio is decided only after every track; a later ``vide`` wins."""
    target = write_ftyp(tmp_path / "x.mp4", "isom", ("isom",), ("soun", "vide"))

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(details(result)).contains("hdlr=soun", "hdlr=vide")


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
    assert_that(details(result)).contains(f"major_brand={brand}")


@pytest.mark.parametrize("brand", ["F4V ", "M4A ", "isom"])
def test_ftyp_without_moov_is_unknown(tmp_path: Path, brand: str) -> None:
    """A non-image ``ftyp`` with no ``moov`` does not fall back to the name."""
    target = write_ftyp(tmp_path / "clip.mp4", brand, (brand,), tracks=None)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.extension_media_type).is_equal_to(MediaType.VIDEO)
    assert_that(decision(result).detail).contains("no moov")


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
    assert_that(details(result)).contains("hdlr=soun")


def test_ftyp_after_free(tmp_path: Path) -> None:
    """``ftyp`` need not be the first box."""
    target = tmp_path / "x.bin"
    target.write_bytes(box(b"free", b"\x00" * 8) + ftyp("heic", ("mif1", "heic")))

    assert_that(detect_media_type(target)).is_equal_to(MediaType.IMAGE)


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
    assert_that(decision(result).detail).starts_with("decision=quicktime")


# --- box order (carried item 1) ---------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected", "reason"),
    [
        (
            moov(("vide",)) + ftyp("avis", ("avis", "msf1")),
            MediaType.IMAGE,
            "image-sequence brand",
        ),
        (moov(()) + ftyp("heic", ("mif1", "heic")), MediaType.IMAGE, "image brand"),
        (
            moov(("soun",)) + ftyp("M4A ", ("M4A ", "heic")),
            None,
            "unknown conflict: image and audio brands",
        ),
        (
            moov(("vide",)) + ftyp("M4V ", ("M4V ",)),
            MediaType.VIDEO,
            "track handler vide",
        ),
        (
            moov(("vide", "vide"), extra=canon_uuid(CR3_PROFILE))
            + ftyp("crx ", ("crx ",)),
            MediaType.IMAGE,
            "canon CR3 profile",
        ),
    ],
    ids=[
        "vide-then-avis",
        "empty-then-heic",
        "soun-then-m4a-heic",
        "vide-then-m4v",
        "cr3",
    ],
)
def test_ftyp_after_moov_is_still_read(
    tmp_path: Path,
    data: bytes,
    expected: MediaType | None,
    reason: str,
) -> None:
    """The walk keeps looking for ``ftyp`` after ``moov``; order never decides."""
    target = tmp_path / "x.bin"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(result.major_brand).is_not_none()
    assert_that(decision(result).detail).is_equal_to(f"decision=iso-bmff {reason}")


def test_cr3_profile_recorded_when_moov_comes_first(tmp_path: Path) -> None:
    """A CR3 profile read before ``ftyp`` decides image, not video."""
    target = tmp_path / "x.cr3"
    target.write_bytes(
        moov(("vide",), extra=canon_uuid(CR3_PROFILE)) + ftyp("crx ", ("crx ", "isom")),
    )

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(details(result)).contains(
        "hdlr=vide", "brand_family=canon-crx brands=crx "
    )


@pytest.mark.parametrize(
    "tail",
    [
        struct.pack(">I", 1 << 20) + b"mdat" + b"\x00" * 16,
        b"\x00\x00\x00",
        struct.pack(">I", 3) + b"free",
    ],
    ids=["mdat-past-eof", "truncated-header", "size-3"],
)
def test_malformed_tail_after_legacy_moov_ends_the_walk(
    tmp_path: Path,
    tail: bytes,
) -> None:
    """With ``moov`` read and no ``ftyp`` seen, a malformed tail is not fatal."""
    target = tmp_path / "old.mov"
    target.write_bytes(moov(("vide", "soun")) + tail)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(result.container).is_equal_to("quicktime")
    assert_that(decision(result).detail).is_equal_to(
        "decision=quicktime track handler vide",
    )


def test_malformed_ftyp_after_moov_is_malformed(tmp_path: Path) -> None:
    """A cut ``ftyp`` after ``moov`` has been seen, so the file is malformed."""
    target = tmp_path / "old.mov"
    target.write_bytes(moov(("vide",)) + ftyp("isom")[:12])

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(decision(result).detail).contains(
        "malformed: box extends past the file"
    )


# --- Canon CRX ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["IMG_0001.CR3", "renamed.bin"])
def test_canon_cr3_profile_is_image(tmp_path: Path, name: str) -> None:
    """An established CR3 profile is an image despite its ``vide`` tracks."""
    result = detect(write_canon(tmp_path / name, CR3_PROFILE))

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(details(result)).contains(
        "hdlr=vide",
        "canon_cncv=CanonCR3_001/00.09.00/00.00.00",
        "decision=iso-bmff canon CR3 profile",
    )


@pytest.mark.parametrize("name", ["MVI_0001.CRM", "renamed.cr3", "renamed.bin"])
def test_canon_crm_profile_is_video(tmp_path: Path, name: str) -> None:
    """An established CRM profile is video, even named ``.cr3``."""
    result = detect(write_canon(tmp_path / name, CRM_PROFILE))

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(decision(result).detail).contains("canon CRM profile")


@pytest.mark.parametrize("name", ["IMG_0001.cr3", "renamed.bin"])
def test_canon_crx_without_profile_is_unknown(tmp_path: Path, name: str) -> None:
    """A bare ``crx`` brand lacks evidence and never falls back to ``.cr3``."""
    result = detect(write_canon(tmp_path / name, None))

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains("insufficient-evidence")
    assert_that(details(result)).contains("brand_family=canon-crx brands=crx ")


def test_canon_crx_ftyp_only_is_unknown(tmp_path: Path) -> None:
    """A ``crx`` ``ftyp`` with nothing after it is unknown."""
    target = tmp_path / "x.cr3"
    target.write_bytes(ftyp("crx ", ("crx ", "isom")))

    assert_that(detect_media_type(target)).is_none()


def test_canon_unsupported_profile_is_unknown(tmp_path: Path) -> None:
    """A Canon profile that is neither CR3 nor CRM is unknown."""
    result = detect(write_canon(tmp_path / "x.cr3", b"CanonXYZ/1"))

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains("unsupported")


def test_canon_uuid_with_other_usertype_is_ignored(tmp_path: Path) -> None:
    """Only the documented Canon usertype is searched for ``CNCV``."""
    other = box(b"uuid", b"\x11" * 16 + box(b"CNCV", CR3_PROFILE))
    data = ftyp("crx ", ("crx ",)) + moov(("vide",), extra=other)
    target = tmp_path / "x.cr3"
    target.write_bytes(data)

    assert_that(detect_media_type(target)).is_none()
