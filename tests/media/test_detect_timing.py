"""Worst-case time of the text-based signatures (#393).

Each case fills the 64 KiB head with the input most likely to make a
signature's scan backtrack or repeat work, and requires ``detect`` to
return well within a deadline. On 7aa40b3 the playlist and XBM regular
expressions, and the SVG check behind the playlist check, ran past 30 s.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.detect_support import detect_with_deadline
from winnow.media._packet_streams import MAX_FLV_TAGS, MAX_PROGRAM_STREAM_PACKETS

LIMIT_SECONDS = 1.0
"""Longest a single ``detect`` call may take on these inputs."""

_HEAD = 64 * 1024

_HOSTILE: dict[str, bytes] = {
    "playlist-comment-runs": b"<!--" + b"--><!--" * 9300,
    "playlist-unclosed-comments": b"<!--" * 16000,
    "playlist-xml-declarations": b'<?xml version="1.0"?>' * 3000,
    "playlist-spaces": b" " * (_HEAD - 8) + b"<asx",
    "xspf-long-tag": b"<playlist " + b"a" * (_HEAD - 20),
    "svg-comments": b"<!---->" * 9300 + b"<svg/>",
    "svg-instructions": b"<?a?>" * 13000 + b"<svg/>",
    "svg-doctype-brackets": b"<!DOCTYPE [" + b"[" * (_HEAD - 20),
    "svg-unclosed-comment": b"<!--" + b"-" * (_HEAD - 8),
    "xbm-comment-runs": b"#define a_width 1\n/*" + b"*//*" * 16300,
    "xbm-unclosed-comment": b"#define a_width 1\n/*" + b"*" * (_HEAD - 30),
    "xbm-long-name": b"#define " + b"a" * (_HEAD - 20) + b"_widt",
    "xbm-bits-search": b"#define a_width 1\n#define a_height 1\n" + b"char \t" * 10900,
    "eps-long-line": b"%!PS-Adobe-3.0" + b" " * (_HEAD - 20),
    "xpm-long": b"/* XPM */" + b"\x00" * (_HEAD - 10),
    "netpbm-comments": b"P6" + b"\n#" * 32000,
    "netpbm-unended-comment": b"P6 #" + b"x" * (_HEAD - 10),
    "netpbm-spaces": b"P6" + b" " * (_HEAD - 4),
    "pam-header": b"P7\n" + b"WIDTH " * 10900,
    "xbm-defines-and-comments": (b"/* c */\n#define a_width 1 /* x */\n" * 2000),
    "xbm-define-without-newline": b"#define a_width " + b"1" * (_HEAD - 20),
    "xbm-bits-far-away": b"#define a_width 1\n#define a_height 1\n" + b"a_bit" * 13000,
    "svg-prefixed-long-name": b"<" + b"a" * 60 + b":svg " + b"x" * (_HEAD - 80),
    "svg-prefixed-no-tag-end": b'<p:svg xmlns:p="' + b"a" * (_HEAD - 20),
    "doctype-quotes": b"<!DOCTYPE svg " + b'"a" ' * 16000,
    "doctype-brackets": b"<!DOCTYPE svg " + b"[" * (_HEAD - 20),
    "doctype-comments": b"<!DOCTYPE svg [" + b"<!---->" * 9000,
    "y4m-header-without-newline": b"YUV4MPEG2 " + b"W1 H1 " * 10900,
    "y4m-many-parameters": b"YUV4MPEG2 " + b"W1 " * 1300 + b"\nFRAME\n",
    "amr-printable-frames": b"#!AMR\n" + (b"$" + b"A" * 19) * 3300,
    "amr-no-data-frames": b"#!AMR\n" + b"|" * (_HEAD - 6),
    "xmf-length": b"XMF_1.00" + b"\xff" * (_HEAD - 8),
    "plain-text": b"word " * 13100,
    "plain-text-multibyte": "é".encode() * 32000,
}


_HOSTILE_NAMED: dict[str, tuple[str, bytes]] = {
    "dxf-comment-pairs": ("x.dxf", b"999\n" * 16000),
    "dxf-long-line": ("x.dxf", b" " * (_HEAD - 4)),
    "xpm1-long-line": ("x.xpm", b"#define " + b"a" * (_HEAD - 10)),
    "svg-xml-start-spaces": ("x.svg", b" " * (_HEAD - 8) + b"<?xml"),
    "669-control-message": ("x.669", b"if" + b"\x01" * (_HEAD - 2)),
    "fpx-directory-far": ("x.fpx", bytes.fromhex("d0cf11e0a1b11ae1") + b"\xff" * 64),
    "pict-header": ("x.pict", b"\x00" * _HEAD),
}
"""Inputs for the checks that run only under their own extension."""


@pytest.mark.parametrize("name", sorted(_HOSTILE_NAMED))
def test_named_check_worst_case_time(tmp_path: Path, name: str) -> None:
    """Every extension-only and text-media marker check runs in bounded time."""
    file_name, data = _HOSTILE_NAMED[name]
    target = tmp_path / file_name
    target.write_bytes(data[:_HEAD])

    start = time.perf_counter()
    detect_with_deadline(target)
    elapsed = time.perf_counter() - start

    assert_that(elapsed).described_as(name).is_less_than(LIMIT_SECONDS)


def _empty_audio_packets(count: int) -> bytes:
    """Build a program stream of a pack, a system header and empty packets.

    Args:
        count: Empty audio PES packets.

    Returns:
        The stream.
    """
    pack = b"\x00\x00\x01\xba\x44\x00\x04\x00\x04\x01\x01\x89\xc3\xf8"
    system = b"\x00\x00\x01\xbb\x00\x06\x80\x00\x01\x04\x20\xff"
    return pack + system + b"\x00\x00\x01\xc0\x00\x00" * count


def _empty_flv_tags(count: int) -> bytes:
    """Build an FLV of empty audio tags.

    Args:
        count: Tags.

    Returns:
        The file.
    """
    tag = b"\x08" + b"\x00" * 10 + b"\x00\x00\x00\x0b"
    return b"FLV\x01\x04\x00\x00\x00\x09" + b"\x00" * 4 + tag * count


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("x.mpg", _empty_audio_packets(MAX_PROGRAM_STREAM_PACKETS + 8)),
        ("x.flv", _empty_flv_tags(MAX_FLV_TAGS + 8)),
    ],
    ids=["program-stream-cap", "flv-cap"],
)
def test_header_walks_at_their_caps_are_fast(
    tmp_path: Path, name: str, data: bytes
) -> None:
    """Walking the most headers the caps allow stays within the deadline."""
    target = tmp_path / name
    target.write_bytes(data)

    start = time.perf_counter()
    detect_with_deadline(target)
    elapsed = time.perf_counter() - start

    assert_that(elapsed).is_less_than(LIMIT_SECONDS)


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_text_signature_worst_case_time(tmp_path: Path, name: str) -> None:
    """Every text-based signature runs in bounded time on hostile input."""
    target = tmp_path / "hostile.bin"
    target.write_bytes(_HOSTILE[name][:_HEAD])

    start = time.perf_counter()
    detect_with_deadline(target)
    elapsed = time.perf_counter() - start

    assert_that(elapsed).described_as(name).is_less_than(LIMIT_SECONDS)
