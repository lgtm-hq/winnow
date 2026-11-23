"""Library formats and playlists under the ruling for PR B (#393).

Each format in ruling item 1 is typed from its content under its own
extension, ``.bin``, no extension and a wrong media extension. Playlists
(ruling item 3) are unknown under every name. Also the two automated
findings on 1a8d05c: XBM spacing and later PMT versions.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.library_support import LIBRARY_SAMPLES, PLAYLIST_SAMPLES
from winnow.media import _stream_probe
from winnow.media._detect import DetectionResult, detect
from winnow.media.registry import (
    create_default_format_registry,
    media_type_for_extension,
)
from winnow.models.media import MediaType


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ffprobe absent so only the bytes decide.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)


def _detect_bytes(tmp_path: Path, data: bytes, name: str) -> DetectionResult:
    """Write ``data`` and detect it.

    Args:
        tmp_path: Directory to write in.
        data: File bytes.
        name: File name.

    Returns:
        The result.
    """
    target = tmp_path / name
    target.write_bytes(data)
    return detect(target)


def _details(result: DetectionResult) -> list[str]:
    """Return every signal detail.

    Args:
        result: Detection result.

    Returns:
        The details.
    """
    return [signal.detail for signal in result.signals]


@pytest.mark.parametrize(
    ("extension", "data", "container", "expected"),
    [pytest.param(e, d, c, m, id=n) for n, e, d, c, m in LIBRARY_SAMPLES],
)
@pytest.mark.parametrize("naming", ["own", "bin", "none", "wrong"])
def test_library_format_under_any_name(
    tmp_path: Path,
    extension: str,
    data: bytes,
    container: str,
    expected: MediaType,
    naming: str,
) -> None:
    """Every ruling item 1 format is typed from its content, under any name."""
    wrong = "mp3" if expected == MediaType.IMAGE else "jpg"
    name = {"own": f"x.{extension}", "bin": "x.bin", "none": "x", "wrong": f"x.{wrong}"}

    result = _detect_bytes(tmp_path, data, name[naming])

    assert_that(result.media_type).is_equal_to(expected)
    assert_that(result.container).is_equal_to(container)


def test_jpeg_xl_container_is_not_claimed_by_iso_bmff(tmp_path: Path) -> None:
    """The JPEG XL signature box is not an ISO-BMFF opening box."""
    data = next(d for n, _, d, _, _ in LIBRARY_SAMPLES if n == "jxl")

    result = _detect_bytes(tmp_path, data, "photo.bin")

    assert_that(result.container).is_equal_to("jpeg-xl")
    assert_that(result.major_brand).is_none()
    assert_that([s.source for s in result.signals]).does_not_contain("ftyp")


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\x0a\xfa\x7f" + b"\x00" * 32,
        b"\x00\x00\x00\x0cJXL \r\n\x87\n" + b"\x00" * 16,
    ],
    ids=["bare-codestream", "signature-without-ftyp"],
)
def test_jpeg_xl_without_a_defensible_signature(tmp_path: Path, data: bytes) -> None:
    """The bare codestream and a signature box without ``ftyp`` are not typed."""
    result = _detect_bytes(tmp_path, data, "photo.bin")

    assert_that(result.media_type).is_none()


@pytest.mark.parametrize(
    "data",
    [
        b"\x00" * 1080 + b"M.K.",
        b"\x00" * 950 + b"\x00" + b"\x00" * 129 + b"M.K." + b"\x00" * 64,
        b"PSID\x00\x09\x00\x7c" + b"\x00" * 64,
        b"PSF\x99" + b"\x00" * 16,
        b"PSF\x01" + struct.pack("<II", 4096, 0) + b"\x00" * 8,
        b"8BPS\x00\x03" + b"\x00" * 32,
        b"Xcur" + struct.pack("<II", 20, 0x10000),
        b"\xd7\xcd\xc6\x9a" + b"\x00" * 18,
        b"\x01\x00\x00\x00" + b"\x00" * 60,
    ],
    ids=[
        "mod-cut",
        "mod-song-length-0",
        "sid-version-9",
        "psf-unknown-version",
        "psf-program-past-file",
        "psd-version-3",
        "xcursor-header-20",
        "wmf-bad-checksum",
        "emf-no-signature",
    ],
)
def test_near_misses_are_not_typed(tmp_path: Path, data: bytes) -> None:
    """Magic without the structure the specification requires is not typed."""
    assert_that(_detect_bytes(tmp_path, data, "x.bin").media_type).is_none()


# --- ruling item 3: playlists ------------------------------------------------


@pytest.mark.parametrize(
    ("extension", "data"),
    [pytest.param(e, d, id=n) for n, e, d in PLAYLIST_SAMPLES],
)
@pytest.mark.parametrize("naming", ["own", "bin", "none", "mp3"])
def test_playlists_are_never_media(
    tmp_path: Path,
    extension: str,
    data: bytes,
    naming: str,
) -> None:
    """A playlist is unknown under every name, its own extension included."""
    name = {
        "own": f"list.{extension}",
        "bin": "list.bin",
        "none": "list",
        "mp3": "list.mp3",
    }

    result = _detect_bytes(tmp_path, data, name[naming])

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_in("playlist", "text")


@pytest.mark.parametrize(
    "extension", ["m3u", "pls", "asx", "ram", "wax", "mxu", "xspf"]
)
def test_playlist_extension_is_non_media_with_a_signal(
    tmp_path: Path, extension: str
) -> None:
    """Unrecognised bytes under a playlist extension say why they are unknown."""
    result = _detect_bytes(tmp_path, b"\x01\x02\x03 binary\x00", f"list.{extension}")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("playlist")
    assert_that(_details(result)).contains(
        "decision=playlist non-media: playlist extension, a playlist is not media",
    )


def test_media_named_as_a_playlist_is_still_media(tmp_path: Path) -> None:
    """Content recognised as media wins over a playlist extension."""
    data = next(d for n, _, d, _, _ in LIBRARY_SAMPLES if n == "sid")

    assert_that(_detect_bytes(tmp_path, data, "tune.m3u").media_type).is_equal_to(
        MediaType.AUDIO
    )


@pytest.mark.parametrize(
    ("root", "container"),
    [
        (b'<playlist version="1" xmlns="http://xspf.org/ns/0/">', "playlist"),
        (b"<playlist xmlns='http://xspf.org/ns/0/' version='1'>", "playlist"),
        (b'<playlist xmlns:x="http://xspf.org/ns/0/">', "playlist"),
        (b'<playlist note="see xspf.org for the format">', "text"),
        (b'<playlist xmlns="http://xspf.org.example/ns/0/">', "text"),
    ],
    ids=["double-quoted", "single-quoted", "prefixed", "mention", "other-host"],
)
def test_xspf_needs_the_exact_namespace(
    tmp_path: Path, root: bytes, container: str
) -> None:
    """A ``<playlist`` root is XSPF only with the XSPF namespace as a value."""
    data = b'<?xml version="1.0"?>\n' + root + b"\n</playlist>\n"

    result = _detect_bytes(tmp_path, data, "list.bin")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to(container)


def test_playlist_fifo_is_non_media(tmp_path: Path) -> None:
    """A playlist extension on a path that is not read is still non-media."""
    result = detect(tmp_path / "gone.m3u")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("playlist")


def test_user_override_still_wins_for_a_playlist_extension(tmp_path: Path) -> None:
    """A user's own registration for ``.m3u`` outranks the playlist rule."""
    registry = create_default_format_registry()
    registry.register(extension="m3u", media_type=MediaType.AUDIO)
    target = tmp_path / "list.m3u"
    target.write_bytes(b"#EXTM3U\nsong.mp3\n")

    assert_that(detect(target, registry=registry).media_type).is_equal_to(
        MediaType.AUDIO
    )


def test_registry_entries_for_playlists_are_unchanged() -> None:
    """The registry's name lookup is not changed by the playlist rule."""
    registry = create_default_format_registry(use_mime_fallback=False)

    assert_that(media_type_for_extension("m3u", registry=registry)).is_none()
    assert_that(registry.formats).does_not_contain_key("m3u")


# --- automated findings on 1a8d05c -------------------------------------------


@pytest.mark.parametrize(
    "between",
    [b"\n", b"\n\n", b"/* hot spot below */\n", b"\n/* a\n multi-line comment */\n"],
    ids=["none", "blank-line", "comment", "multi-line-comment"],
)
def test_xbm_with_space_between_defines(tmp_path: Path, between: bytes) -> None:
    """Blank lines and comments between the defines keep an XBM an image."""
    data = b"#define img_width 8" + between + b"#define img_height 1\n"
    data += b"static unsigned char img_bits[] = { 0x00 };\n"

    result = _detect_bytes(tmp_path, data, "icon.bin")

    assert_that(result.media_type).is_equal_to(MediaType.IMAGE)


def _ts_packet(pid: int, section: bytes) -> bytes:
    """Build a transport packet starting a PSI section.

    Args:
        pid: Packet identifier.
        section: The section.

    Returns:
        The packet.
    """
    data = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10, 0x00]) + section
    return data + b"\xff" * (188 - len(data))


def _psi(table_id: int, extension: int, body: bytes, *, version: int = 0) -> bytes:
    """Build a current PSI section with a dummy CRC.

    Args:
        table_id: Table ID.
        extension: ``transport_stream_id`` or ``program_number``.
        body: Bytes after ``last_section_number``.
        version: ``version_number``.

    Returns:
        The section.
    """
    rest = (
        struct.pack(">HBBB", extension, 0xC1 | (version << 1), 0, 0)
        + body
        + b"\x00" * 4
    )
    return bytes([table_id, 0xB0 | (len(rest) >> 8), len(rest) & 0xFF]) + rest


def test_later_pmt_version_with_video_is_kept(tmp_path: Path) -> None:
    """A new PMT version that adds video replaces the audio-only one."""
    pat = _psi(0x00, 1, struct.pack(">HHHH", 1, 0xE100, 2, 0xE200))
    audio_v0 = _psi(0x02, 1, b"\xe1\x01\xf0\x00\x0f\xe1\x01\xf0\x00", version=0)
    video_v1 = _psi(
        0x02, 1, b"\xe1\x01\xf0\x00\x0f\xe1\x01\xf0\x00\x1b\xe1\x02\xf0\x00", version=1
    )
    packets = [
        _ts_packet(0, pat),
        _ts_packet(0x100, audio_v0),
        _ts_packet(0x100, video_v1),
        *[b"\x47\x1f\xff\x10" + b"\xff" * 184] * 5,
    ]

    result = _detect_bytes(tmp_path, b"".join(packets), "x.ts")

    assert_that(result.media_type).is_equal_to(MediaType.VIDEO)
