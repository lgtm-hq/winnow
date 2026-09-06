"""Tests for Live Photo pair detection.

Pairing rules are exercised through injected dict-backed identifier readers so
no EXIF or ffprobe access is needed. One test builds a synthetic Apple
MakerNote JPEG to drive the real still reader, and one builds a tiny MOV with
ffmpeg (skipped when the binary is absent) to drive the real video reader.
"""

from __future__ import annotations

import shutil
import struct
import subprocess  # nosec B404 - fixed argv list to build a test fixture
from collections.abc import Mapping
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.classify.conftest import ImageFactory
from winnow.classify.livephoto import (
    APPLE_CONTENT_IDENTIFIER_TAG,
    IdentifierReader,
    LivePhotoPair,
    LivePhotoScan,
    detect_live_photos,
    find_live_photo_pairs,
    still_content_identifier,
    video_content_identifier,
)

_UUID = "A1B2C3D4-E5F6-4711-8899-AABBCCDDEEFF"
_ROOT = Path("/library")


def _reader(mapping: Mapping[Path, str | None]) -> IdentifierReader:
    """Build an identifier reader backed by a dict.

    Args:
        mapping: Path to identifier; missing paths read as ``None``.

    Returns:
        A reader callable suitable for ``find_live_photo_pairs``.
    """
    return lambda path: mapping.get(path)


def _scan(
    stills: Mapping[Path, str | None],
    videos: Mapping[Path, str | None],
) -> LivePhotoScan:
    """Run the pure pairing over dict-backed readers.

    Args:
        stills: Still paths mapped to identifiers.
        videos: Video paths mapped to identifiers.

    Returns:
        The resulting scan.
    """
    return find_live_photo_pairs(
        [*stills, *videos],
        still_identifier=_reader(stills),
        video_identifier=_reader(videos),
    )


def apple_maker_note(identifier: str) -> bytes:
    """Build an Apple-style MakerNote carrying a content identifier.

    Args:
        identifier: ASCII identifier stored in ``Tag 0x0011``.

    Returns:
        Raw MakerNote bytes suitable for EXIF tag ``0x927C``.
    """
    payload = identifier.encode("ascii") + b"\x00"
    entry = struct.pack(">HHII", 0x0011, 2, len(payload), 2 + 12 + 4)
    ifd = struct.pack(">H", 1) + entry + struct.pack(">I", 0)
    return b"Apple iOS\x00" + b"\x00\x01" + b"MM" + ifd + payload


def test_verified_pair_by_identifier_ignores_stem() -> None:
    """Equal identifiers pair a still and video even with different stems."""
    still = _ROOT / "IMG_0001.HEIC"
    video = _ROOT / "clips" / "IMG_9999.MOV"

    scan = _scan({still: _UUID}, {video: _UUID})

    assert_that(scan.pairs).is_equal_to(
        (
            LivePhotoPair(
                still=still,
                video=video,
                content_identifier=_UUID,
                verified=True,
            ),
        ),
    )
    assert_that(scan.unpaired_stills).is_empty()
    assert_that(scan.unpaired_videos).is_empty()


def test_equal_stems_with_different_identifiers_do_not_pair() -> None:
    """Matching stems are not enough when both identifiers disagree."""
    still = _ROOT / "IMG_0001.JPG"
    video = _ROOT / "IMG_0001.MOV"

    scan = _scan({still: _UUID}, {video: "other"})

    assert_that(scan.pairs).is_empty()
    assert_that(scan.unpaired_stills).is_equal_to((still,))
    assert_that(scan.unpaired_videos).is_equal_to((video,))


@pytest.mark.parametrize(
    ("still_id", "video_id"),
    [(None, _UUID), (_UUID, None), (None, None)],
    ids=["still_missing", "video_missing", "both_missing"],
)
def test_equal_stems_with_missing_identifier_form_unverified_pair(
    still_id: str | None,
    video_id: str | None,
) -> None:
    """Same-directory equal stems pair unverified when an identifier is absent."""
    still = _ROOT / "img_0001.jpeg"
    video = _ROOT / "IMG_0001.MOV"

    scan = _scan({still: still_id}, {video: video_id})

    assert_that(scan.pairs).is_length(1)
    pair = scan.pairs[0]
    assert_that(pair.verified).is_false()
    assert_that(pair.content_identifier).is_none()
    assert_that(pair.still).is_equal_to(still)
    assert_that(pair.video).is_equal_to(video)


def test_equal_stems_in_different_directories_do_not_pair_unverified() -> None:
    """Stem matching is restricted to a single directory."""
    still = _ROOT / "a" / "IMG_0001.JPG"
    video = _ROOT / "b" / "IMG_0001.MOV"

    scan = _scan({still: None}, {video: None})

    assert_that(scan.pairs).is_empty()


def test_orphans_on_both_sides_are_reported_sorted() -> None:
    """Unmatched stills and videos are listed as sorted orphans."""
    stills = {_ROOT / "z.jpg": None, _ROOT / "a.heic": "id-a"}
    videos = {_ROOT / "y.mov": None, _ROOT / "b.mov": "id-b"}

    scan = _scan(stills, videos)

    assert_that(scan.pairs).is_empty()
    assert_that(scan.unpaired_stills).is_equal_to((_ROOT / "a.heic", _ROOT / "z.jpg"))
    assert_that(scan.unpaired_videos).is_equal_to((_ROOT / "b.mov", _ROOT / "y.mov"))


def test_non_candidate_suffixes_are_ignored() -> None:
    """Files outside the still and video suffix sets never appear in the scan."""
    scan = find_live_photo_pairs(
        [_ROOT / "notes.txt", _ROOT / "clip.mp4", _ROOT / "image.png"],
        still_identifier=_reader({}),
        video_identifier=_reader({}),
    )

    assert_that(scan).is_equal_to(
        LivePhotoScan(pairs=(), unpaired_stills=(), unpaired_videos=()),
    )


def test_pairs_are_ordered_verified_first_then_by_still() -> None:
    """Verified pairs precede unverified ones; each group sorts by still path."""
    stills = {
        _ROOT / "c.jpg": "id-c",
        _ROOT / "a.jpg": "id-a",
        _ROOT / "d.jpg": None,
        _ROOT / "b.jpg": None,
    }
    videos = {
        _ROOT / "x.mov": "id-c",
        _ROOT / "y.mov": "id-a",
        _ROOT / "d.mov": None,
        _ROOT / "b.mov": None,
    }

    scan = _scan(stills, videos)

    assert_that([pair.still.name for pair in scan.pairs]).is_equal_to(
        ["a.jpg", "c.jpg", "b.jpg", "d.jpg"],
    )
    assert_that([pair.verified for pair in scan.pairs]).is_equal_to(
        [True, True, False, False],
    )


def test_shared_identifier_prefers_same_stem_then_lexicographic() -> None:
    """Within one identifier, same-stem twins pair first; leftovers are orphans."""
    stills = {_ROOT / "a.jpg": _UUID, _ROOT / "b.jpg": _UUID, _ROOT / "c.jpg": _UUID}
    videos = {_ROOT / "b.mov": _UUID, _ROOT / "z.mov": _UUID}

    scan = _scan(stills, videos)

    assert_that([(p.still.name, p.video.name) for p in scan.pairs]).is_equal_to(
        [("a.jpg", "z.mov"), ("b.jpg", "b.mov")],
    )
    assert_that(scan.unpaired_stills).is_equal_to((_ROOT / "c.jpg",))
    assert_that(scan.unpaired_videos).is_empty()


def test_still_content_identifier_reads_synthetic_maker_note(
    make_image: ImageFactory,
) -> None:
    """The real still reader extracts the UUID from an Apple MakerNote JPEG."""
    path = make_image(
        name="IMG_0001.JPG",
        exif={0x010F: "Apple"},
        exif_ifd={0x927C: apple_maker_note(_UUID)},
    )

    assert_that(still_content_identifier(path)).is_equal_to(_UUID)
    assert_that(APPLE_CONTENT_IDENTIFIER_TAG).is_equal_to("Tag 0x0011")


def test_still_content_identifier_none_without_maker_note(
    make_image: ImageFactory,
) -> None:
    """A JPEG without a MakerNote yields no identifier."""
    path = make_image(name="plain.jpg", exif={0x010F: "Apple"})

    assert_that(still_content_identifier(path)).is_none()


def test_video_content_identifier_none_without_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ffprobe makes the video reader return None."""
    video = tmp_path / "clip.mov"
    video.write_bytes(b"fake")
    monkeypatch.setattr("winnow.media.video.shutil.which", lambda name: None)

    assert_that(video_content_identifier(video)).is_none()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required to build and probe the MOV fixture",
)
def test_detect_live_photos_pairs_real_files(
    make_image: ImageFactory,
    tmp_path: Path,
) -> None:
    """End-to-end: a synthetic JPEG and ffmpeg-built MOV form a verified pair."""
    still = make_image(
        name="IMG_0001.JPG",
        exif={0x010F: "Apple"},
        exif_ifd={0x927C: apple_maker_note(_UUID)},
    )
    video = tmp_path / "IMG_0001.MOV"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not found")
    subprocess.run(  # nosec B603 - fixed argv list from which(); no shell
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=10:d=0.2",
            "-metadata",
            f"com.apple.quicktime.content.identifier={_UUID}",
            "-movflags",
            "use_metadata_tags",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    scan = detect_live_photos(tmp_path)

    assert_that(scan.pairs).is_equal_to(
        (
            LivePhotoPair(
                still=still,
                video=video,
                content_identifier=_UUID,
                verified=True,
            ),
        ),
    )


def test_detect_live_photos_non_recursive_skips_subdirectories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-recursive scans only consider the top-level directory."""
    (tmp_path / "top.jpg").write_bytes(b"")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "deep.jpg").write_bytes(b"")
    monkeypatch.setattr(
        "winnow.classify.livephoto.read_maker_note_tags",
        lambda path: {},
    )

    shallow = detect_live_photos(tmp_path, recursive=False)
    deep = detect_live_photos(tmp_path, recursive=True)

    assert_that(shallow.unpaired_stills).is_equal_to((tmp_path / "top.jpg",))
    assert_that(deep.unpaired_stills).is_equal_to(
        (nested / "deep.jpg", tmp_path / "top.jpg"),
    )
