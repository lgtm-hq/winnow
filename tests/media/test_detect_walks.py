"""Tests for the completeness of container stream walks (#393).

Audio is decided only from a complete stream list; these cases cover how
each walk knows it read the whole list, and what happens when it did not.
"""

from __future__ import annotations

import struct
import uuid
from pathlib import Path

import pytest
from assertpy import assert_that

from tests.media.ebml_support import (
    INFO,
    SEEK,
    SEEK_HEAD,
    SEEK_ID,
    TRACKS,
    VOID,
    cluster,
    element,
    matroska,
)
from tests.media.signature_support import avi, riff, riff_chunk
from winnow.media import _containers, _stream_probe
from winnow.media._containers import asf_streams, avi_streams, realmedia_streams
from winnow.media._detect import DetectionResult, detect
from winnow.media._ffprobe import BoundedRun
from winnow.models.media import MediaType


def _guid(text: str) -> bytes:
    """Encode a GUID as ASF stores it.

    Args:
        text: GUID text.

    Returns:
        Its 16 bytes.
    """
    return uuid.UUID(text).bytes_le


_HEADER = _guid("75b22630-668e-11cf-a6d9-00aa0062ce6c")
_PROPERTIES = _guid("b7dc0791-a9b7-11cf-8ee6-00c00c205365")
_EXTENSION = _guid("5fbf03b5-a92e-11cf-8ee3-00c00c205365")
_EXTENDED = _guid("14e6a5cb-c672-4332-8399-a96952065b5a")
_AUDIO = _guid("f8699e40-5b4d-11cf-a8fd-00805f5c442b")
_VIDEO = _guid("bc19efc0-5b4d-11cf-a8fd-00805f5c442b")


def _object(guid: bytes, body: bytes) -> bytes:
    """Build an ASF object.

    Args:
        guid: Object GUID.
        body: Object data.

    Returns:
        The object.
    """
    return guid + struct.pack("<Q", 24 + len(body)) + body


def _properties(kind: bytes, number: int) -> bytes:
    """Build a Stream Properties Object (ASF specification 3.3).

    Args:
        kind: Stream type GUID.
        number: Stream number.

    Returns:
        The object.
    """
    return _object(
        _PROPERTIES, kind + b"\x00" * 32 + struct.pack("<H", number) + b"\x00" * 4
    )


def _extended(
    number: int, embedded: bytes = b"", *, names: int = 1, systems: int = 1
) -> bytes:
    """Build an Extended Stream Properties Object (ASF specification 4.1).

    Args:
        number: Stream number.
        embedded: An embedded Stream Properties Object.
        names: Stream names to include.
        systems: Payload extension systems to include.

    Returns:
        The object.
    """
    fixed = b"\x00" * 48 + struct.pack("<HH", number, 0) + b"\x00" * 8
    fixed += struct.pack("<HH", names, systems)
    body = fixed + b"\x00\x00\x02\x00ab" * names
    body += (b"\x22" * 16 + struct.pack("<HI", 0, 3) + b"xyz") * systems
    return _object(_EXTENDED, body + embedded)


def _overrun(*, names: int = 0, systems: int = 0) -> bytes:
    """Build an Extended Stream Properties whose counts overrun the object.

    Args:
        names: Stream names declared (none present).
        systems: Payload extension systems declared (none present).

    Returns:
        The object.
    """
    fixed = b"\x00" * 48 + struct.pack("<HH", 3, 0) + b"\x00" * 8
    return _object(_EXTENDED, fixed + struct.pack("<HH", names, systems) + b"\x00\x00")


def _asf(*objects: bytes) -> bytes:
    """Build an ASF Header Object around ``objects``.

    Args:
        objects: Header objects.

    Returns:
        The file bytes.
    """
    body = b"".join(objects)
    size = struct.pack("<Q", 30 + len(body))
    return _HEADER + size + struct.pack("<I", len(objects)) + b"\x01\x02" + body


def _extension(*objects: bytes) -> bytes:
    """Build a Header Extension Object (ASF specification 3.4).

    Args:
        objects: Objects in its data.

    Returns:
        The object.
    """
    data = b"".join(objects)
    return _object(
        _EXTENSION, b"\x00" * 16 + b"\x06\x00" + struct.pack("<I", len(data)) + data
    )


@pytest.mark.parametrize(
    ("data", "kinds", "complete"),
    [
        (
            _asf(
                _properties(_AUDIO, 1), _extension(_extended(2, _properties(_VIDEO, 2)))
            ),
            ["audio", "video"],
            True,
        ),
        (_asf(_properties(_AUDIO, 1), _extension(_extended(1))), ["audio"], True),
        (_asf(_properties(_AUDIO, 1), _extension(_extended(2))), ["audio"], False),
        (_asf(_object(_PROPERTIES, b"\x00" * 10)), [], False),
        (_asf(_extension(_object(_EXTENDED, b"\x00" * 20))), [], False),
        (_asf(_extension(_overrun(names=5))), [], False),
        (_asf(_extension(_overrun(systems=5))), [], False),
        (_asf(_properties(_AUDIO, 1))[:-4], [], False),
    ],
    ids=[
        "embedded-video",
        "declared-and-typed",
        "declared-not-typed",
        "short-properties",
        "short-extended",
        "cut-names",
        "cut-systems",
        "cut-header",
    ],
)
def test_asf_stream_list(data: bytes, kinds: list[str], complete: bool) -> None:
    """Every stream an Extended Stream Properties declares must be typed."""
    streams = asf_streams(data)

    assert_that(streams.kinds).is_equal_to(kinds)
    assert_that(streams.complete).is_equal_to(complete)


def test_avi_movi_before_hdrl_is_incomplete() -> None:
    """An AVI without ``hdrl`` before ``movi`` lists nothing."""
    data = riff(
        b"RIFF", b"AVI ", riff_chunk(b"LIST", b"movi") + riff_chunk(b"LIST", b"hdrl")
    )

    streams = avi_streams(data)

    assert_that(streams.kinds).is_empty()
    assert_that(streams.complete).is_false()


@pytest.mark.parametrize("cap", [1, 3])
def test_avi_chunk_cap_makes_the_list_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
) -> None:
    """A walk stopped by the chunk cap is incomplete."""
    monkeypatch.setattr(_containers, "MAX_CHUNKS", cap)

    streams = avi_streams(avi(b"auds", b"auds", b"auds"))

    assert_that(streams.complete).is_false()


def test_avi_chunk_cap_inside_a_stream_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chunk cap also stops the walk inside ``LIST strl``."""
    monkeypatch.setattr(_containers, "MAX_CHUNKS", 1)
    strl = riff_chunk(
        b"LIST",
        b"strl"
        + riff_chunk(b"strh", b"auds" + b"\x00" * 52)
        + riff_chunk(b"strf", b""),
    )
    data = riff(b"RIFF", b"AVI ", riff_chunk(b"LIST", b"hdrl" + strl))

    assert_that(avi_streams(data).complete).is_false()


def test_short_pat_section_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PAT section too short to hold its header leaves the PAT unread."""
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)
    section = b"\x00\xb0\x05\x00\x01\xc1\x00\x00"
    packet = b"\x47\x40\x00\x10\x00" + section
    packets = [packet + b"\xff" * (188 - len(packet))]
    packets += [b"\x47\x1f\xff\x10" + b"\xff" * 184] * 5

    result = _detect_bytes(tmp_path, b"".join(packets), "x.ts")

    assert_that(result.media_type).is_none()
    assert_that(result.container).is_equal_to("mpeg-ts")


def test_realmedia_chunk_smaller_than_its_header_stops() -> None:
    """A chunk size below ten bytes ends the walk, incomplete."""
    data = (
        b".RMF"
        + struct.pack(">IH", 18, 0)
        + b"\x00" * 8
        + b"PROP"
        + struct.pack(">IH", 4, 0)
    )

    assert_that(realmedia_streams(data).complete).is_false()


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


@pytest.mark.parametrize(
    ("stdout", "expected", "reason"),
    [
        (
            b'{"streams": [{"codec_type": "audio"}]}',
            MediaType.AUDIO,
            "stream types audio from ffprobe",
        ),
        (b'{"streams": []}', None, "unknown unresolved: no audio or video stream"),
    ],
    ids=["probe-audio", "probe-nothing"],
)
def test_incomplete_list_asks_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    expected: MediaType | None,
    reason: str,
) -> None:
    """An incomplete stream list is settled by ffprobe when it is there."""
    monkeypatch.setattr(
        _stream_probe, "find_binary", lambda _: "/opt/ffmpeg/bin/ffprobe"
    )
    monkeypatch.setattr(_stream_probe, "fd_protocol_supported", lambda _: True)
    run = BoundedRun(
        returncode=0, stdout=stdout, stderr=b"", timed_out=False, overflowed=False
    )
    monkeypatch.setattr(_stream_probe, "run_bounded", lambda *_, **__: run)
    data = riff(b"RIFF", b"AVI ", riff_chunk(b"LIST", b"movi"))

    result = _detect_bytes(tmp_path, data, "x.avi")

    assert_that(result.media_type).is_equal_to(expected)
    assert_that([s.detail for s in result.signals]).contains("streams=incomplete")
    decisions = [s.detail for s in result.signals if s.detail.startswith("decision=")]
    assert_that(decisions[0]).contains(reason)


def test_complete_list_without_media_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete list of data streams is unknown without asking ffprobe."""

    def _forbidden(*_: object, **__: object) -> None:
        raise AssertionError("ffprobe was asked")

    monkeypatch.setattr("winnow.media._signatures.probe_stream_types", _forbidden)

    result = _detect_bytes(tmp_path, avi(b"txts"), "x.avi")

    assert_that(result.media_type).is_none()
    assert_that([s.detail for s in result.signals]).contains("streams=complete")


# --- Matroska SeekHead and master elements -------------------------------------


@pytest.mark.parametrize(
    "seek_head",
    [
        element(SEEK_HEAD, element(VOID) + element(SEEK, element(SEEK_ID, INFO))),
        element(SEEK_HEAD, element(SEEK, element(SEEK_ID, TRACKS))),
        element(SEEK_HEAD, element(SEEK, element(VOID))),
    ],
    ids=["other-target", "no-position", "no-id"],
)
def test_seekhead_without_a_tracks_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seek_head: bytes,
) -> None:
    """A ``SeekHead`` that gives no ``Tracks`` position settles nothing."""
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)

    result = _detect_bytes(tmp_path, matroska(seek_head, cluster()), "x.mkv")

    assert_that(result.media_type).is_none()
    assert_that([s.detail for s in result.signals]).contains(
        "tracks=unresolved: no Tracks before a Cluster",
    )


def test_tracks_of_unknown_size_is_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Tracks`` may not have an unknown size."""
    monkeypatch.setattr(_stream_probe, "find_binary", lambda _: None)
    unknown = TRACKS + b"\x01\xff\xff\xff\xff\xff\xff\xff" + element(b"\xae")

    result = _detect_bytes(tmp_path, matroska(unknown), "x.mkv")

    assert_that(result.media_type).is_none()
    assert_that([s.detail for s in result.signals]).contains(
        "tracks=unresolved malformed: master element of unknown size",
    )
