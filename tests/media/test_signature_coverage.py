"""Formats ``main`` typed correctly that the second review found missing (#393).

Each sample is a minimal well-formed file (Pillow writes the EPS), checked
under its own extension, ``.bin``, no extension, and a wrong media
extension. Every case failed on 66eb9d5.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from tests.media.signature_support import dts14_frames, dts_le_frames, riff
from winnow.media._detect import detect
from winnow.models.media import MediaType


def _eps(path: Path) -> None:
    """Write an EPS with Pillow.

    Args:
        path: Destination.
    """
    Image.new("RGB", (8, 6), (1, 2, 3)).save(path, format="EPS")


def _bytes(data: bytes) -> Callable[[Path], None]:
    """Return a writer of fixed bytes.

    Args:
        data: The file.

    Returns:
        The writer.
    """

    def write(path: Path) -> None:
        path.write_bytes(data)

    return write


_SVG_PROLOG = (
    b'<?xml version="1.0"?>\n'
    + b"<!-- "
    + b"long comment " * 900
    + b"-->\n"
    + b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>\n'
)
_FITS = (b"SIMPLE  =" + b" " * 20 + b"T").ljust(
    80
) + b"BITPIX  =                    8".ljust(80)
_FITS += b"NAXIS   =                    0".ljust(80) + b"END".ljust(80)
_FITS = _FITS.ljust(2880)
_MIDI = (
    b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
    + b"MTrk\x00\x00\x00\x04\x00\xff\x2f\x00"
)
_AU_LE = b"dns." + struct.pack("<5I", 24, 64, 2, 8000, 1) + b"\x00" * 64

_CASES: list[tuple[str, Callable[[Path], None], MediaType]] = [
    ("eps", _eps, MediaType.IMAGE),
    ("ape", _bytes(b"MAC \x96\x0f\x00\x00" + b"\x00" * 64), MediaType.AUDIO),
    ("mpc", _bytes(b"MP+\x17" + b"\x00" * 64), MediaType.AUDIO),
    ("mpc", _bytes(b"MPCKSH\x10" + b"\x00" * 64), MediaType.AUDIO),
    (
        "xcf",
        _bytes(b"gimp xcf file\x00" + struct.pack(">III", 8, 6, 0) + b"\x00" * 32),
        MediaType.IMAGE,
    ),
    ("jxr", _bytes(b"II\xbc\x01\x08\x00\x00\x00" + b"\x00" * 64), MediaType.IMAGE),
    ("wdp", _bytes(b"II\xbc\x01\x08\x00\x00\x00" + b"\x00" * 64), MediaType.IMAGE),
    (
        "lbm",
        _bytes(b"FORM\x00\x00\x00\x2cILBMBMHD\x00\x00\x00\x14" + b"\x00" * 28),
        MediaType.IMAGE,
    ),
    (
        "rmi",
        _bytes(riff(b"RIFF", b"RMID", b"data" + struct.pack("<I", len(_MIDI)) + _MIDI)),
        MediaType.AUDIO,
    ),
    ("svg", _bytes(_SVG_PROLOG), MediaType.IMAGE),
    ("mid", _bytes(_MIDI), MediaType.AUDIO),
    ("fits", _bytes(_FITS), MediaType.IMAGE),
    (
        "djvu",
        _bytes(b"AT&TFORM\x00\x00\x00\x1aDJVUINFO\x00\x00\x00\x0a" + b"\x00" * 10),
        MediaType.IMAGE,
    ),
    ("dv", _bytes((b"\x1f\x07\x00\x3f" + b"\x00" * 76) * 12), MediaType.VIDEO),
    ("xpm", _bytes(b"! XPM2\n1 1 1 1\na c #000000\na\n"), MediaType.IMAGE),
    ("au", _bytes(_AU_LE), MediaType.AUDIO),
    ("dts", _bytes(dts_le_frames()), MediaType.AUDIO),
    ("dts", _bytes(dts14_frames()), MediaType.AUDIO),
    ("dts", _bytes(dts14_frames(little=True)), MediaType.AUDIO),
]


def _ids() -> list[str]:
    """Name each case uniquely.

    Returns:
        The ids.
    """
    seen: dict[str, int] = {}
    ids = []
    for extension, _, _ in _CASES:
        seen[extension] = seen.get(extension, 0) + 1
        ids.append(f"{extension}-{seen[extension]}")
    return ids


@pytest.mark.parametrize(("extension", "write", "expected"), _CASES, ids=_ids())
@pytest.mark.parametrize("naming", ["own", "bin", "none", "wrong"])
def test_format_is_typed_under_any_name(
    tmp_path: Path,
    extension: str,
    write: Callable[[Path], None],
    expected: MediaType,
    naming: str,
) -> None:
    """Content decides these formats, under any name."""
    wrong = "mp3" if expected == MediaType.IMAGE else "jpg"
    name = {"own": f"x.{extension}", "bin": "x.bin", "none": "x", "wrong": f"x.{wrong}"}
    target = tmp_path / name[naming]
    write(target)

    result = detect(target)

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_not_none()
