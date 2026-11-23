"""Tests for rule 2's bounds, malformed boxes and claiming rules (#393).

Header-only cases are tiny literal box structures and assert detection only.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.bmff_support import box, ftyp, large_box, moov, track
from tests.media.detect_support import decision, details
from winnow.media import _detect_bmff
from winnow.media._detect import TRACK_BOX, detect
from winnow.media.registry import detect_media_type
from winnow.models.media import MediaType

# --- bounds and malformed structures ----------------------------------------


def test_detect_truncated_ftyp_falls_through(tmp_path: Path) -> None:
    """Fewer bytes than one box header is not ISO-BMFF; the name decides."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftyp"[:6])

    result = detect(target)

    assert_that(result.container).is_none()
    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(decision(result).source).is_equal_to("extension")


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (ftyp("isom", ("isom",))[:18], "box extends past the file"),
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
    assert_that(decision(result).detail).contains("malformed").contains(reason)


def test_detect_nesting_loop_hits_depth_cap(tmp_path: Path) -> None:
    """Containers nested past the depth cap are over budget, not followed."""
    nested = box(TRACK_BOX, box(b"mdia", b""))
    for _ in range(_detect_bmff.MAX_BOX_DEPTH + 2):
        nested = box(b"moov", nested)
    target = tmp_path / "deep.mp4"
    target.write_bytes(ftyp("isom", ("isom",)) + nested)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains("budget-exceeded: box depth")


def test_detect_box_count_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More boxes than the cap are over budget."""
    monkeypatch.setattr(_detect_bmff, "MAX_BOXES", 16)
    target = tmp_path / "many.mp4"
    target.write_bytes(
        ftyp("isom", ("isom",)) + box(b"free") * 32 + moov(("soun",)),
    )

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains("budget-exceeded: box count")


def test_detect_read_budget_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading more than the byte budget is over budget."""
    monkeypatch.setattr(_detect_bmff, "MAX_BYTES_READ", 64)
    target = tmp_path / "many.mp4"
    target.write_bytes(ftyp("isom", ("isom",)) + box(b"free") * 16 + moov(("soun",)))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains("budget-exceeded: read budget")


def test_large_ftyp_is_over_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid ``ftyp`` larger than its read budget is not malformed."""
    monkeypatch.setattr(_detect_bmff, "MAX_FTYP_PAYLOAD_BYTES", 16)
    target = tmp_path / "x.heic"
    target.write_bytes(ftyp("heic", ("mif1", "heic", "miaf")))

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(decision(result).detail).contains(
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
    assert_that(decision(result).detail).contains("ftyp shorter")


def test_size_zero_box_extends_to_end(tmp_path: Path) -> None:
    """A size-0 ``mdat`` before ``moov`` runs to the end and swallows it."""
    target = tmp_path / "x.mp4"
    target.write_bytes(
        ftyp("isom", ("isom",)) + b"\x00\x00\x00\x00mdat" + b"x" * 40 + moov(("soun",)),
    )

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(decision(result).detail).is_equal_to(
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
    assert_that(decision(result).detail).is_equal_to(reason)


def test_size_zero_moov_extends_to_end(tmp_path: Path) -> None:
    """A size-0 ``moov`` is walked to the end of the file."""
    target = tmp_path / "x.mp4"
    body = moov(("vide",))[8:]
    target.write_bytes(ftyp("isom", ("isom",)) + b"\x00\x00\x00\x00moov" + body)

    assert_that(detect_media_type(target)).is_equal_to(MediaType.VIDEO)


# --- which files rule 2 claims ------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("image.ppm", b"P3\n#free\n1 1\n255\n255 0 0\n"),
        ("image.bin", b"P3\n#free\n1 1\n255\n255 0 0\n"),
        ("image.ppm", b"P3\n#ftyp\n1 1\n255\n255 0 0\n"),
        (
            "image.svg",
            b"<!--free--><svg xmlns='http://www.w3.org/2000/svg' width='1'"
            b" height='1'/>",
        ),
        (
            "image.bin",
            b"<!--ftyp--><svg xmlns='http://www.w3.org/2000/svg' width='1'"
            b" height='1'/>",
        ),
    ],
    ids=["ppm-free", "ppm-free-bin", "ppm-ftyp", "svg-free", "svg-ftyp-bin"],
)
def test_text_image_with_box_word_is_an_image(
    tmp_path: Path,
    name: str,
    data: bytes,
) -> None:
    """A text image holding ``free`` or ``ftyp`` at offset 4 stays an image."""
    target = tmp_path / name
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(result.container).is_in("netpbm", "svg")
    sources = {s.source for s in result.signals}
    assert_that(sources).does_not_contain("ftyp", "track-handler")


@pytest.mark.parametrize(
    "data",
    [
        b"ab: moov is a word, not a box\n",
        b"\x00\x00\x00\x07mdat and seven is too small",
        b"\x00\x00\x00\x01wide\x00\x00\x00\x00\x00\x00\x00\x08x",
        b"\x00\x00\x00\x01skip\x00\x00\x00",
        struct.pack(">I", 0xFFFFFFFF) + b"ftypisom",
        struct.pack(">I", 8 + _detect_bmff.MAX_FTYP_PAYLOAD_BYTES + 1) + b"ftypisom",
    ],
    ids=[
        "moov-huge",
        "mdat-size-7",
        "wide-large-8",
        "skip-large-cut",
        "ftyp-0xffffffff",
        "ftyp-over-budget-size",
    ],
)
def test_implausible_first_box_is_not_claimed(tmp_path: Path, data: bytes) -> None:
    """Only a plausible first box size claims a file for rule 2."""
    target = tmp_path / "notes.mp4"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.container).is_not_in("iso-bmff", "quicktime")
    sources = {s.source for s in result.signals}
    assert_that(sources).does_not_contain("ftyp", "track-handler")


@pytest.mark.parametrize(
    "data",
    [
        b"\x00\x00\x00\x00ftypisom\x00\x00\x00\x00",
        large_box(b"ftyp", b"isom\x00\x00\x00\x00"),
        box(b"ftyp", b"isom\x00\x00\x00\x00isom"),
        struct.pack(">I", 4) + b"ftypisom\x00\x00\x00\x00",
        struct.pack(">I", 8 + _detect_bmff.MAX_FTYP_PAYLOAD_BYTES) + b"ftypisom",
    ],
    ids=["size-0", "size-1-large", "complete", "short", "at-budget"],
)
def test_ftyp_size_within_budget_is_claimed(tmp_path: Path, data: bytes) -> None:
    """``ftyp`` sized 0, 1 or within its budget is claimed, short or cut."""
    target = tmp_path / "x.bin"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.container).is_equal_to("iso-bmff")


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


@pytest.mark.parametrize("name", ["clip.mov", "clip.bin"])
def test_truncated_legacy_quicktime_is_not_claimed(tmp_path: Path, name: str) -> None:
    """A first ``moov`` declaring more than the file holds is not claimed.

    Decision for the review of pull request 449: ruling item 3 claims a
    non-``ftyp`` first box only with a plausible size, so a cut legacy
    QuickTime file falls through to the later rules and, with nothing else
    recognised, to rule 5 (``puremagic`` agreeing with the extension) or
    the extension itself.
    """
    data = moov(("vide",))
    target = tmp_path / name
    target.write_bytes(struct.pack(">I", len(data) + 4096) + data[4:])

    result = detect(target)

    assert_that(result.container).is_none()
    assert_that(decision(result).source).is_in("puremagic", "extension")
    assert_that(result.media_type).is_equal_to(result.extension_media_type)


@pytest.mark.parametrize("name", ["seg-1.m4s", "seg-1.bin", "seg-1.mp4"])
@pytest.mark.parametrize("first", [b"styp", b"moof"])
def test_standalone_media_segment_is_unknown(
    tmp_path: Path,
    name: str,
    first: bytes,
) -> None:
    """A media segment without ``moov`` is unknown whatever its name.

    Decision for the review of pull request 449: a segment names no track
    type, so it is claimed and unknown, and ``.mp4`` does not make it video.
    """
    opening = box(b"styp", b"msdh\x00\x00\x00\x00msdhmsix") if first == b"styp" else b""
    fragment = box(b"moof", box(b"mfhd", b"\x00" * 8)) + box(b"mdat", b"\x00" * 32)
    target = tmp_path / name
    target.write_bytes(opening + fragment)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(decision(result).detail).is_equal_to(
        "decision=iso-bmff unknown unresolved: media segment without moov",
    )


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (box(b"ftyp"), "malformed: ftyp shorter than its minimum size"),
        (
            large_box(b"ftyp", b"isom\x00\x00\x00\x00" + b"isom" * 1025),
            "budget-exceeded: ftyp larger than its read budget",
        ),
        (
            struct.pack(">I", 4) + b"ftypisom\x00\x00\x00\x00",
            "malformed: non-progressing",
        ),
    ],
    ids=["8-byte-ftyp", "1025-brands-large-size", "size-4-ftyp"],
)
def test_bad_ftyp_is_labelled_iso_bmff(
    tmp_path: Path,
    data: bytes,
    reason: str,
) -> None:
    """An ``ftyp`` seen but not read still names the container."""
    target = tmp_path / "clip.mp4"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("iso-bmff")
    assert_that(decision(result).source).is_equal_to("ftyp")
    assert_that(decision(result).detail).starts_with("decision=iso-bmff unknown")
    assert_that(decision(result).detail).contains(reason)
    assert_that(details(result)).contains("ftyp=unread")


def test_nested_moov_track_is_not_a_track(tmp_path: Path) -> None:
    """A track box inside a nested ``moov`` is not a track."""
    nested = box(b"moov", track("vide"))
    data = ftyp("isom", ("isom",)) + box(
        b"moov",
        box(b"mvhd", b"\x00" * 16) + nested + track("soun"),
    )
    target = tmp_path / "x.mp4"
    target.write_bytes(data)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(details(result)).contains("hdlr=soun").does_not_contain("hdlr=vide")


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
    """The opening bytes count against the read budget."""
    data = _tight_file()
    target = tmp_path / "x.mp4"
    target.write_bytes(data)
    monkeypatch.setattr(_detect_bmff, "MAX_BYTES_READ", len(data))

    over = detect(target)
    monkeypatch.setattr(
        _detect_bmff,
        "MAX_BYTES_READ",
        len(data) + _detect_bmff.HEAD_BYTES,
    )
    within = detect(target)

    assert_that(over.media_type).is_none()
    assert_that(decision(over).detail).contains("budget-exceeded: read budget")
    assert_that(within.media_type).is_equal_to(MediaType.AUDIO)
