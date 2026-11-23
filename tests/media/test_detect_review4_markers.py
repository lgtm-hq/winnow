"""Extension-only and text-media markers: third review of PR 454 (#393).

Ruling item 2 as amended: an extension-only format is typed only when its
extension and its marker agree, and plain text under a text-media name
needs its own marker. Each finding's tests failed on 3db4394. ffprobe is
absent throughout.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.media import _detect, _stream_probe
from winnow.media._detect import DetectionResult, detect
from winnow.media.registry import FormatRegistry, create_default_format_registry
from winnow.models.media import MediaType

AUDIO = MediaType.AUDIO
IMAGE = MediaType.IMAGE
VIDEO = MediaType.VIDEO


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ffprobe absent.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)


def _detect_bytes(
    tmp_path: Path,
    data: bytes,
    name: str = "x.bin",
    *,
    registry: FormatRegistry | None = None,
) -> DetectionResult:
    """Write ``data`` and detect it.

    Args:
        tmp_path: Directory to write in.
        data: File bytes.
        name: File name.
        registry: Registry to detect with.

    Returns:
        The result.
    """
    target = tmp_path / name
    target.write_bytes(data)
    return detect(target, registry=registry)


def _pseudo_random(length: int) -> bytes:
    """Return fixed bytes that look random (SHA-256 in counter mode).

    Args:
        length: Bytes wanted.

    Returns:
        The bytes.
    """
    blocks = (hashlib.sha256(b"393-%d" % i).digest() for i in range(length // 32 + 1))
    return b"".join(blocks)[:length]


# --- blocker 2: extension-only formats need their marker ---------------------


def _ole(clsid: bytes) -> bytes:
    """Build a compound file whose root entry has ``clsid``.

    Args:
        clsid: Root storage CLSID, 16 bytes.

    Returns:
        Header sector and one directory sector.
    """
    header = bytearray(512)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    header[0x1A:0x20] = struct.pack("<HHH", 0x3E, 3, 0xFFFE)
    header[0x1E:0x20] = struct.pack("<H", 9)
    header[0x30:0x34] = struct.pack("<I", 0)
    directory = bytearray(512)
    directory[:20] = "Root Entry".encode("utf-16-le")
    directory[0x50:0x60] = clsid
    return bytes(header + directory)


_FLASHPIX_CLSID = bytes.fromhex("0067615654c1ce11855300aa00a1f95b")
_VISIO_CLSID = bytes.fromhex("1a0d02000000000000c0000000000046")


def _composer_669(
    counts: bytes = b"\x01\x01\x00",
    first_order: int = 0,
    tempo: int = 4,
    row: int = 0x3F,
) -> bytes:
    """Build a Composer 669 module: one sample, one pattern.

    Args:
        counts: Samples, patterns and restart position.
        first_order: The first order entry.
        tempo: Every tempo entry.
        row: Every break row.

    Returns:
        The module.
    """
    header = b"if" + b"a 669 song".ljust(108) + counts
    orders = bytes([first_order]) + b"\xff" * 127
    tempos, breaks = bytes([tempo]) * 128, bytes([row]) * 128
    return header + orders + tempos + breaks + b"\x00" * (25 + 1536)


def _flic(magic: int) -> bytes:
    """Build a FLIC header.

    Args:
        magic: ``0xAF11`` or ``0xAF12``.

    Returns:
        The file.
    """
    return struct.pack("<IHHHHH", 128, magic, 1, 320, 200, 8) + b"\x00" * 114


def _pict() -> bytes:
    """Build a PICT file: header, size, frame, version 2 opcode."""
    return (
        b"\x00" * 512 + b"\x00\x0a" + b"\x00" * 8 + b"\x00\x11\x02\xff" + b"\x00" * 64
    )


def _tga() -> bytes:
    """Build a footerless TGA: one 24-bit true-colour pixel."""
    return (
        struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 1, 1, 24, 0) + b"\x00" * 3
    )


def _dib() -> bytes:
    """Build a packed DIB: BITMAPINFOHEADER then one pixel."""
    return struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00" * 4


_EPS = b"%!PS-Adobe-3.0\n%%BoundingBox: 0 0 1 1\nshowpage\n"
_FIG = b"#FIG 3.2  Produced by xfig version 3.2.8\nLandscape\nCenter\nInches\n"

_MARKED: dict[str, tuple[bytes, MediaType]] = {
    "669": (_composer_669(), AUDIO),
    "koz": (b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64, AUDIO),
    "fli": (_flic(0xAF11), VIDEO),
    "flc": (_flic(0xAF12), VIDEO),
    "jxl": (b"\xff\x0a" + b"\x00\xfa\x7f\x01" + b"\x00" * 60, IMAGE),
    "fpx": (_ole(_FLASHPIX_CLSID), IMAGE),
    "eps": (_EPS, IMAGE),
    "epsf": (_EPS, IMAGE),
    "epsi": (b"%!PS-Adobe-3.0 EPSI\n%%BeginPreview: 1 1 1 1\n", IMAGE),
    "ai": (b"%!PS-Adobe-3.0\n%%Creator: Adobe Illustrator(R) 8.0\n%%EOF\n", IMAGE),
    "fig": (_FIG, IMAGE),
    "sk": (b"##Sketch 1 2\ndocument()\nlayout('A4',0)\n", IMAGE),
    "sk1": (b"##Sketch 1 2\ndocument()\nlayout('A4',0)\n", IMAGE),
    "ag": (b"*BEGIN GRAPHICS VERSION=430/420 ENCODING=7BIT\n", IMAGE),
    "dwg": (b"AC1018" + b"\x00" * 5 + b"\x01\x00" + b"\x00" * 64, IMAGE),
    "dxf": (b"999\nmade by hand\n  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n", IMAGE),
    "pict": (_pict(), IMAGE),
    "pct": (_pict(), IMAGE),
    "pic": (_pict(), IMAGE),
    "tga": (_tga(), IMAGE),
    "dib": (_dib(), IMAGE),
}
"""Each extension-only format with its marker and ``main``'s type (literal)."""

_REGISTRIES: dict[str, Callable[[], FormatRegistry]] = {
    "host-mime": create_default_format_registry,
    "no-mime": lambda: create_default_format_registry(use_mime_fallback=False),
}

_MATLAB_FIG = (
    b"MATLAB 5.0 MAT-file, Platform: MACA64, Created on: Mon Nov 23 12:00:00 2026"
).ljust(116)
_MATLAB_FIG += b"\x00" * 8 + b"\x00\x01IM" + b"\x0e\x00\x00\x00\x30\x00\x00\x00"
_MATLAB_FIG += b"\x06\x00\x00\x00\x08\x00\x00\x00" + b"\x02" + b"\x00" * 64

_PROSE = b"Meeting notes: bring the slides, and the budget for next year.\n" * 4
_RANDOM = _pseudo_random(4096)


@pytest.mark.parametrize("extension", sorted(_MARKED))
def test_every_prefix_and_fuzzed_tail_is_safe(tmp_path: Path, extension: str) -> None:
    """Prefixes and pseudo-random tails of each marker file never raise."""
    data, _ = _MARKED[extension]
    target = tmp_path / f"x.{extension}"
    for length in range(len(data) + 1):
        target.write_bytes(data[:length])
        detect(target)
    for case in range(64):
        target.write_bytes(data[: case * 8] + _pseudo_random(512 + case))
        detect(target)


@pytest.mark.parametrize("registry", sorted(_REGISTRIES))
@pytest.mark.parametrize("extension", sorted(_MARKED))
def test_extension_only_marker_gives_the_type(
    tmp_path: Path, extension: str, registry: str
) -> None:
    """With its marker, each extension-only format has ``main``'s type."""
    data, expected = _MARKED[extension]
    result = _detect_bytes(
        tmp_path, data, f"x.{extension}", registry=_REGISTRIES[registry]()
    )

    assert_that(result.media_type).is_equal_to(expected)


def _other_format(extension: str) -> bytes:
    """Return another extension-only format's file for ``extension``.

    Args:
        extension: The extension under test.

    Returns:
        xfig for a binary format's extension, DWG for the others.
    """
    if extension in {"fig", "eps", "epsf", "epsi", "ai", "sk", "sk1", "ag", "dxf"}:
        return _MARKED["dwg"][0]
    return _FIG


@pytest.mark.parametrize("registry", sorted(_REGISTRIES))
@pytest.mark.parametrize("kind", ["prose", "random", "matlab", "other-format"])
@pytest.mark.parametrize("extension", sorted(_MARKED))
def test_extension_only_without_its_marker_is_unknown(
    tmp_path: Path, extension: str, kind: str, registry: str
) -> None:
    """Prose, random bytes, a MATLAB file or another format give unknown."""
    data = {
        "prose": _PROSE,
        "random": _RANDOM,
        "matlab": _MATLAB_FIG,
        "other-format": _other_format(extension),
    }[kind]
    result = _detect_bytes(
        tmp_path, data, f"x.{extension}", registry=_REGISTRIES[registry]()
    )

    assert_that(result.media_type).is_none()


@pytest.mark.parametrize("registry", sorted(_REGISTRIES))
@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("plot.fig", _MATLAB_FIG),
        ("plan.vst", _ole(_VISIO_CLSID)),
        ("deck.fpx", _ole(_VISIO_CLSID)),
    ],
    ids=["matlab-figure", "visio-template", "office-file-as-fpx"],
)
def test_realistic_non_media_under_item_2_names(
    tmp_path: Path, name: str, data: bytes, registry: str
) -> None:
    """A MATLAB figure and a Visio template are not media."""
    result = _detect_bytes(tmp_path, data, name, registry=_REGISTRIES[registry]())

    assert_that(result.media_type).is_none()


def test_flashpix_directory_past_the_head(tmp_path: Path) -> None:
    """A FlashPix root entry past the opening bytes is read within budget."""
    data = bytearray(_ole(_FLASHPIX_CLSID)[:512])
    data[0x30:0x34] = struct.pack("<I", 200)
    data += b"\x00" * (201 * 512 - 512)
    directory = bytearray(512)
    directory[0x50:0x60] = _FLASHPIX_CLSID
    data += directory

    assert_that(_detect_bytes(tmp_path, bytes(data), "x.fpx").media_type).is_equal_to(
        IMAGE
    )


@pytest.mark.parametrize(
    "data",
    [
        _ole(_FLASHPIX_CLSID)[:0x30],
        _ole(_FLASHPIX_CLSID)[:0x1E] + b"\x07\x00" + _ole(_FLASHPIX_CLSID)[0x20:],
        _ole(_FLASHPIX_CLSID)[:0x30] + b"\x00\x10\x00\x00" + b"\x00" * 64,
    ],
    ids=["cut-header", "bad-sector-shift", "directory-past-the-end"],
)
def test_broken_flashpix_is_unknown(tmp_path: Path, data: bytes) -> None:
    """A compound file header that cannot lead to the root entry is unknown."""
    assert_that(_detect_bytes(tmp_path, data, "x.fpx").media_type).is_none()


def test_flashpix_directory_past_the_read_budget_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root entry the spent read budget cannot reach is unknown."""
    monkeypatch.setattr(_detect, "SIGNATURE_BYTES_READ", 64 * 1024 + 32)
    data = bytearray(_ole(_FLASHPIX_CLSID)[:512])
    data[0x30:0x34] = struct.pack("<I", 200)
    data += b"\x00" * (201 * 512 - 512)
    directory = bytearray(512)
    directory[0x50:0x60] = _FLASHPIX_CLSID
    data += directory

    assert_that(_detect_bytes(tmp_path, bytes(data), "x.fpx").media_type).is_none()


@pytest.mark.parametrize("name", ["x.tga", "x.dib"], ids=["tga-short", "dib-short"])
def test_files_shorter_than_their_header_are_unknown(tmp_path: Path, name: str) -> None:
    """A binary file shorter than a TGA or DIB header is unknown."""
    assert_that(_detect_bytes(tmp_path, b"\x00\x01\x02", name).media_type).is_none()


def test_xbm_unclosed_comment_after_the_last_define(tmp_path: Path) -> None:
    """Six defines then an unclosed comment is not an XBM."""
    data = b"".join(b"#define a_%d 1\n" % index for index in range(6)) + b"/* open"

    assert_that(_detect_bytes(tmp_path, data, "x.xbm").media_type).is_none()


@pytest.mark.parametrize(
    ("extension", "data"),
    [
        ("tga", struct.pack("<BBBHHBHHHHBB", 0, 0, 1, 0, 0, 0, 0, 0, 1, 1, 8, 0)),
        ("tga", struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 4, 0, 0, 0, 1, 1, 24, 0)),
        ("tga", struct.pack("<BBBHHBHHHHBB", 0, 1, 1, 0, 4, 7, 0, 0, 1, 1, 8, 0)),
        ("tga", struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 0, 1, 24, 0)),
        ("tga", struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 1, 1, 12, 0)),
        ("tga", b"\x00\x00\x02"),
        ("dib", struct.pack("<IiiHH", 40, 1, 1, 2, 24)),
        ("dib", struct.pack("<IiiHH", 40, 0, 1, 1, 24)),
        ("dib", struct.pack("<IiiHH", 40, 1, 1, 1, 7)),
        ("dib", struct.pack("<I", 40)),
        ("669", _composer_669(counts=b"\x41\x01\x00")),
        ("669", _composer_669(counts=b"\x01\x81\x00")),
        ("669", _composer_669(counts=b"\x01\x01\x80")),
        ("669", b"if" + b"\x01" * 41 + _composer_669()[43:]),
        ("669", _composer_669(first_order=0x80)),
        ("669", _composer_669(tempo=0)),
        ("669", _composer_669(tempo=16)),
        ("669", _composer_669(row=64)),
        ("669", b"if\x00\xff"),
    ],
    ids=[
        "tga-mapped-without-map",
        "tga-map-spec-without-map",
        "tga-bad-entry-size",
        "tga-zero-width",
        "tga-bad-depth",
        "tga-cut",
        "dib-two-planes",
        "dib-zero-width",
        "dib-bad-depth",
        "dib-cut",
        "669-too-many-samples",
        "669-too-many-patterns",
        "669-bad-restart",
        "669-control-characters",
        "669-bad-order",
        "669-order-without-tempo",
        "669-tempo-too-high",
        "669-break-past-the-pattern",
        "669-cut",
    ],
)
def test_structural_checks_reject_near_misses(
    tmp_path: Path, extension: str, data: bytes
) -> None:
    """Fields outside the specifications' ranges give unknown."""
    padded = data + b"\x00" * 32

    assert_that(_detect_bytes(tmp_path, padded, f"x.{extension}").media_type).is_none()


@pytest.mark.parametrize(
    ("extension", "data", "expected"),
    [
        (
            "tga",
            struct.pack("<BBBHHBHHHHBB", 0, 1, 1, 0, 4, 24, 0, 0, 1, 1, 8, 0),
            IMAGE,
        ),
        ("dib", struct.pack("<IHHHH", 12, 1, 1, 1, 8) + b"\x00" * 8, IMAGE),
        ("dib", struct.pack("<IiiHH", 16, 1, 1, 1, 8), IMAGE),
        (
            "pict",
            b"\x00" * 512 + b"\x00\x0a" + b"\x00" * 8 + b"\x11\x01" + b"\xff",
            IMAGE,
        ),
        ("dxf", b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 16, IMAGE),
        ("eps", b"\xc5\xd0\xd3\xc6" + b"\x00" * 26, IMAGE),
        ("669", _composer_669(counts=b"\x00\x00\x00"), AUDIO),
        ("669", b"JN" + _composer_669(first_order=0xFE)[2:], AUDIO),
    ],
    ids=[
        "tga-colour-mapped",
        "dib-core-header",
        "dib-os2-short-header",
        "pict-version-1",
        "dxf-binary",
        "eps-dos",
        "669-no-patterns",
        "extended-669-skip-order",
    ],
)
def test_other_marker_forms(
    tmp_path: Path, extension: str, data: bytes, expected: MediaType
) -> None:
    """Colour-mapped TGA, a core-header DIB, PICT 1, binary DXF, DOS EPS."""
    assert_that(_detect_bytes(tmp_path, data, f"x.{extension}").media_type).is_equal_to(
        expected
    )


_LONG_PROLOG_SVG = (
    b'<?xml version="1.0"?>\n<!-- ' + b"a long comment " * 4500 + b"-->\n"
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>\n'
)
_XPM1 = (
    b"#define plaid_format 1\n#define plaid_width 2\n#define plaid_height 1\n"
    b"#define plaid_ncolors 1\n#define plaid_chars_per_pixel 1\n"
    b'static char *plaid_colors[] = {" ", "#000000"};\n'
    b'static char *plaid_pixels[] = {"  "};\n'
)
_FITS_F = (b"SIMPLE  = " + b"F".rjust(20)).ljust(80) + b"END".ljust(80) * 35


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("notes.svg", _PROSE, None),
        ("page.svg", b"<html><body>not svg</body></html>\n", None),
        ("long.svg", _LONG_PROLOG_SVG, IMAGE),
        ("long.bin", _LONG_PROLOG_SVG, None),
        ("plaid.xpm", _XPM1, IMAGE),
        ("plaid.bin", _XPM1, None),
        ("notes.xpm", _PROSE, None),
        ("odd.fts", _FITS_F, IMAGE),
        ("odd.fits", _FITS_F, IMAGE),
        ("odd.bin", _FITS_F, IMAGE),
        ("odd.txt", _FITS_F, IMAGE),
        ("notes.fits", _PROSE, None),
        ("notes.fts", _PROSE, None),
        ("notes.fit", _PROSE, None),
        ("notes.xbm", _PROSE, None),
        ("notes.pbm", _PROSE, None),
        ("notes.eps", _PROSE, None),
        ("notes.dxf", _PROSE, None),
    ],
)
def test_plain_text_under_text_media_names_needs_its_marker(
    tmp_path: Path, name: str, data: bytes, expected: MediaType | None
) -> None:
    """Plain text is an image only with its own extension's marker."""
    for registry in _REGISTRIES.values():
        result = _detect_bytes(tmp_path, data, name, registry=registry())

        assert_that(result.media_type).described_as(name).is_equal_to(expected)


def test_xpm1_first_line_must_be_its_format_define(tmp_path: Path) -> None:
    """A long first line, or another define, is not XPM 1."""
    for data in (b"#define plaid_width 2\n" + _XPM1, b"x" * 300 + _XPM1):
        assert_that(_detect_bytes(tmp_path, data, "x.xpm").media_type).is_none()


def test_svg_with_a_bom_and_doctype_first(tmp_path: Path) -> None:
    """A BOM then an SVG DOCTYPE is the XML marker too."""
    data = b"\xef\xbb\xbf  <!DOCTYPE svg PUBLIC" + b" x" * 40000 + b">\n<svg/>\n"

    assert_that(_detect_bytes(tmp_path, data, "x.svg").media_type).is_equal_to(IMAGE)
