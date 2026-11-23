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
    "plain-text": b"word " * 13100,
    "plain-text-multibyte": "é".encode() * 32000,
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_text_signature_worst_case_time(tmp_path: Path, name: str) -> None:
    """Every text-based signature runs in bounded time on hostile input."""
    target = tmp_path / "hostile.bin"
    target.write_bytes(_HOSTILE[name][:_HEAD])

    start = time.perf_counter()
    detect_with_deadline(target)
    elapsed = time.perf_counter() - start

    assert_that(elapsed).described_as(name).is_less_than(LIMIT_SECONDS)
