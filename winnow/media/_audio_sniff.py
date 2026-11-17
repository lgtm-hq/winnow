"""Identify an audio container from its leading bytes, never its file name.

tinytag picks its parser from the file extension before it looks at the
content, so a WAV named ``.mp3`` is parsed as MPEG and gets invented values.
winnow therefore decides the container here, from the bytes alone, and hands
tinytag only an open file object (no name) for containers it parses. Anything
this module cannot establish is left to ffprobe.
"""

from __future__ import annotations

from enum import StrEnum, auto
from io import SEEK_END
from pathlib import Path
from typing import BinaryIO, Final


class AudioContainer(StrEnum):
    """Containers whose tinytag parser winnow trusts."""

    MPEG = auto()
    """MPEG audio frames (MP1, MP2, MP3), with or without an ID3v2 tag."""
    FLAC = auto()
    """Native FLAC, with or without a leading ID3v2 tag."""
    MP4 = auto()
    OGG = auto()
    WAVE = auto()
    ASF = auto()
    AIFF = auto()


_HEADER_BYTES: Final = 36
_ID3_HEADER_BYTES: Final = 10
_ID3_FOOTER_FLAG: Final = 0x10
_MAX_ID3_PADDING: Final = 65536
"""Zero bytes skipped after an ID3v2 tag before the audio must start."""

_ASF_GUID: Final = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")

_MPEG_VERSION_1: Final = 0b11
_MPEG_VERSION_RESERVED: Final = 0b01
_MPEG_LAYER_RESERVED: Final = 0b00
"""Layer bits ``00`` are reserved in MPEG audio and used by ADTS AAC."""
_MPEG_LAYER_1: Final = 0b11

_MPEG_SAMPLE_RATES: Final[dict[int, tuple[int, int, int]]] = {
    0b11: (44100, 48000, 32000),
    0b10: (22050, 24000, 16000),
    0b00: (11025, 12000, 8000),
}
"""Sample rates by version bits (MPEG-1, MPEG-2, MPEG-2.5) and rate index."""

_MPEG_BITRATES_KBPS: Final[dict[tuple[bool, int], tuple[int, ...]]] = {
    (True, 0b11): (
        0,
        32,
        64,
        96,
        128,
        160,
        192,
        224,
        256,
        288,
        320,
        352,
        384,
        416,
        448,
    ),
    (True, 0b10): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 0b01): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 0b11): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 0b10): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 0b01): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
"""Bitrates by (is MPEG-1, layer bits) and bitrate index; index 0 is free format."""


def sniff_audio_container(path: Path) -> AudioContainer | None:
    """Identify the container of an audio file from its content.

    Args:
        path: File to inspect.

    Returns:
        The container, or ``None`` when the content is not one winnow hands to
        tinytag (raw ADTS AAC, AC-3, WavPack, an unreadable file, ...).
    """
    try:
        with path.open("rb") as handle:
            return _sniff(handle)
    except OSError:
        return None


_MAGIC: Final[tuple[tuple[int, bytes, int, tuple[bytes, ...], AudioContainer], ...]] = (
    (0, b"fLaC", 0, (b"",), AudioContainer.FLAC),
    (4, b"ftyp", 0, (b"",), AudioContainer.MP4),
    (0, b"OggS", 0, (b"",), AudioContainer.OGG),
    (0, b"RIFF", 8, (b"WAVE",), AudioContainer.WAVE),
    (0, _ASF_GUID, 0, (b"",), AudioContainer.ASF),
    (0, b"FORM", 8, (b"AIFF", b"AIFC"), AudioContainer.AIFF),
)
"""(offset, signature, second offset, second signatures, container)."""


def _sniff(handle: BinaryIO) -> AudioContainer | None:
    """Identify the container read from an open file positioned at 0."""
    header = handle.read(_HEADER_BYTES)
    if header.startswith(b"ID3"):
        return _sniff_after_id3(handle, header)
    for offset, signature, second_offset, second, container in _MAGIC:
        if header.startswith(signature, offset) and header.startswith(
            second,
            second_offset,
        ):
            return container
    return AudioContainer.MPEG if _is_mpeg_stream(handle, 0) else None


def _sniff_after_id3(handle: BinaryIO, header: bytes) -> AudioContainer | None:
    """Identify what follows an ID3v2 tag: FLAC or MPEG audio frames."""
    if len(header) < _ID3_HEADER_BYTES:
        return None
    size = _syncsafe(header[6:10])
    if size is None:
        return None
    offset = _ID3_HEADER_BYTES + size
    if header[5] & _ID3_FOOTER_FLAG:
        offset += _ID3_HEADER_BYTES
    handle.seek(offset)
    padding = handle.read(_MAX_ID3_PADDING)
    skipped = len(padding) - len(padding.lstrip(b"\x00"))
    offset += skipped
    handle.seek(offset)
    if handle.read(4) == b"fLaC":
        return AudioContainer.FLAC
    if _is_mpeg_stream(handle, offset):
        return AudioContainer.MPEG
    return None


def _syncsafe(data: bytes) -> int | None:
    """Decode an ID3v2 syncsafe integer, or ``None`` if a byte is invalid."""
    if any(byte & 0x80 for byte in data):
        return None
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _is_mpeg_stream(handle: BinaryIO, offset: int) -> bool:
    """Return whether a valid MPEG audio frame, confirmed by the next, starts here.

    A frame is confirmed when the following frame header is also valid, or
    when the file ends exactly where the frame does. Free-format frames
    (bitrate index 0) have no computable length and are accepted on their
    header alone.
    """
    handle.seek(offset)
    length = _mpeg_frame_length(handle.read(4))
    if length is None:
        return False
    if length == 0:
        return True
    end = offset + length
    if handle.seek(0, SEEK_END) == end:
        return True
    handle.seek(end)
    return _mpeg_frame_length(handle.read(4)) is not None


def _mpeg_frame_length(header: bytes) -> int | None:
    """Return an MPEG audio frame's length, 0 for free format, ``None`` if invalid.

    ADTS AAC shares the 12-bit sync word but sets the reserved layer bits
    ``00``, so it is never taken for MPEG audio.
    """
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version = (header[1] >> 3) & 0b11
    layer = (header[1] >> 1) & 0b11
    bitrate_index = header[2] >> 4
    rate_index = (header[2] >> 2) & 0b11
    if (
        version == _MPEG_VERSION_RESERVED
        or layer == _MPEG_LAYER_RESERVED
        or bitrate_index == 0b1111
        or rate_index == 0b11
    ):
        return None
    if bitrate_index == 0:
        return 0
    is_mpeg1 = version == _MPEG_VERSION_1
    bitrate = _MPEG_BITRATES_KBPS[(is_mpeg1, layer)][bitrate_index] * 1000
    sample_rate = _MPEG_SAMPLE_RATES[version][rate_index]
    padding = (header[2] >> 1) & 1
    if layer == _MPEG_LAYER_1:
        return (12 * bitrate // sample_rate + padding) * 4
    if layer == 0b01 and not is_mpeg1:
        return 72 * bitrate // sample_rate + padding
    return 144 * bitrate // sample_rate + padding


def wave_subformat(path: Path) -> int | None:
    """Return the subformat code of a ``WAVE_FORMAT_EXTENSIBLE`` file.

    tinytag reports such files with format code ``FFFE`` whatever they hold;
    the real code (1 for integer PCM, 3 for float) is the first two bytes of
    the subformat GUID at the end of the ``fmt `` chunk.

    Args:
        path: A RIFF WAVE file.

    Returns:
        The subformat code, or ``None`` when the file has no extensible
        ``fmt `` chunk.
    """
    try:
        with path.open("rb") as handle:
            if handle.read(12)[8:12] != b"WAVE":
                return None
            while chunk := handle.read(8):
                if len(chunk) < 8:
                    return None
                size = int.from_bytes(chunk[4:8], "little")
                if chunk[:4] != b"fmt ":
                    handle.seek(size + (size & 1), 1)
                    continue
                body = handle.read(size)
                if len(body) < 26 or int.from_bytes(body[:2], "little") != 0xFFFE:
                    return None
                return int.from_bytes(body[24:26], "little")
    except OSError:
        return None
    return None
