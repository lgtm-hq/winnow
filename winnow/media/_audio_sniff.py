"""Identify an audio container from its leading bytes, never its file name.

tinytag picks its parser from the file extension before it looks at the
content, so a WAV named ``.mp3`` is parsed as MPEG and gets invented values.
winnow therefore decides the container here, from the bytes alone, together
with the offset at which its stream starts, and hands tinytag only an open
file object (no name) positioned there. Anything this module cannot establish
is left to ffprobe.

The module also reads the few structural facts the trust rule in
:mod:`winnow.media.audio` needs and tinytag does not expose: how many audio
tracks an MP4 file holds, and the ``fact`` chunk of a compressed WAV.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class SniffedAudio:
    """A container established from a file's content.

    Attributes:
        container: The container.
        offset: Byte offset at which the container's own data starts: past a
            leading ID3v2 tag and any zero padding for MPEG and FLAC, ``0``
            for the others.
        id3_tag: Whether the file starts with an ID3v2 tag.
    """

    container: AudioContainer
    offset: int = 0
    id3_tag: bool = False


_HEADER_BYTES: Final = 36
_ID3_HEADER_BYTES: Final = 10
_ID3_FOOTER_FLAG: Final = 0x10
_MAX_ID3_TAGS: Final = 8
"""Consecutive ID3v2 tags skipped (some taggers add one without removing one)."""
_MAX_ZERO_PADDING: Final = 1 << 20
"""Zero bytes skipped before the first MPEG frame or the ``fLaC`` marker."""
_READ_CHUNK: Final = 1 << 16

_UTF16_LE_BOM: Final = b"\xff\xfe"
"""Also the start of an MPEG-1 Layer I frame header with a CRC."""
_FRAMES_TO_CONFIRM: Final = 2
_FRAMES_TO_CONFIRM_BOM: Final = 3
"""Frames needed when the file starts like UTF-16 text (``FF FE``)."""

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


def sniff_audio(path: Path) -> SniffedAudio | None:
    """Identify the container of an audio file, and where it starts, from its content.

    Args:
        path: File to inspect.

    Returns:
        The container and its offset, or ``None`` when the content is not one
        winnow hands to tinytag (raw ADTS AAC, AC-3, WavPack, free-format or
        unconfirmed MPEG, an unreadable file, ...).
    """
    try:
        with path.open("rb") as handle:
            return _sniff(handle)
    except OSError:
        return None


def sniff_audio_container(path: Path) -> AudioContainer | None:
    """Identify the container of an audio file from its content.

    Args:
        path: File to inspect.

    Returns:
        The container, or ``None`` when :func:`sniff_audio` establishes none.
    """
    sniffed = sniff_audio(path)
    return None if sniffed is None else sniffed.container


_MAGIC: Final[tuple[tuple[int, bytes, int, tuple[bytes, ...], AudioContainer], ...]] = (
    (0, b"fLaC", 0, (b"",), AudioContainer.FLAC),
    (4, b"ftyp", 0, (b"",), AudioContainer.MP4),
    (0, b"OggS", 0, (b"",), AudioContainer.OGG),
    (0, b"RIFF", 8, (b"WAVE",), AudioContainer.WAVE),
    (0, _ASF_GUID, 0, (b"",), AudioContainer.ASF),
    (0, b"FORM", 8, (b"AIFF", b"AIFC"), AudioContainer.AIFF),
)
"""(offset, signature, second offset, second signatures, container)."""


def _sniff(handle: BinaryIO) -> SniffedAudio | None:
    """Identify the container read from an open file positioned at 0."""
    header = handle.read(_HEADER_BYTES)
    if header.startswith(b"ID3"):
        return _sniff_after_id3(handle, header)
    for offset, signature, second_offset, second, container in _MAGIC:
        if header.startswith(signature, offset) and header.startswith(
            second,
            second_offset,
        ):
            return SniffedAudio(container)
    start = _skip_zeros(handle, 0)
    if start is not None and _is_mpeg_stream(handle, start):
        return SniffedAudio(AudioContainer.MPEG, offset=start)
    return None


def _sniff_after_id3(handle: BinaryIO, header: bytes) -> SniffedAudio | None:
    """Identify what follows one or more ID3v2 tags: FLAC or MPEG audio frames."""
    start = 0
    for _ in range(_MAX_ID3_TAGS):
        after = _skip_id3_tag(handle, start, header)
        if after is None:
            return None
        start = after
        handle.seek(start)
        header = handle.read(_ID3_HEADER_BYTES)
        if not header.startswith(b"ID3"):
            break
    else:
        return None
    if header.startswith(b"fLaC"):
        return SniffedAudio(AudioContainer.FLAC, offset=start, id3_tag=True)
    if _is_mpeg_stream(handle, start):
        return SniffedAudio(AudioContainer.MPEG, offset=start, id3_tag=True)
    return None


def _skip_id3_tag(handle: BinaryIO, offset: int, header: bytes) -> int | None:
    """Return where the data after the ID3v2 tag at ``offset`` (and zeros) starts.

    Returns:
        The offset of the first non-zero byte after the tag, or ``None`` when
        the tag header is invalid or nothing follows it.
    """
    if len(header) < _ID3_HEADER_BYTES:
        return None
    size = _syncsafe(header[6:10])
    if size is None:
        return None
    end = offset + _ID3_HEADER_BYTES + size
    if header[5] & _ID3_FOOTER_FLAG:
        end += _ID3_HEADER_BYTES
    return _skip_zeros(handle, end)


def _skip_zeros(handle: BinaryIO, offset: int) -> int | None:
    """Return the offset of the first non-zero byte at or after ``offset``.

    Returns:
        That offset, or ``None`` when the file ends, or more than
        :data:`_MAX_ZERO_PADDING` zero bytes follow, before one is found.
    """
    handle.seek(offset)
    position = offset
    while position - offset <= _MAX_ZERO_PADDING:
        chunk = handle.read(_READ_CHUNK)
        if not chunk:
            return None
        stripped = chunk.lstrip(b"\x00")
        if stripped:
            found = position + len(chunk) - len(stripped)
            return found if found - offset <= _MAX_ZERO_PADDING else None
        position += len(chunk)
    return None


def _syncsafe(data: bytes) -> int | None:
    """Decode an ID3v2 syncsafe integer, or ``None`` if a byte is invalid."""
    if any(byte & 0x80 for byte in data):
        return None
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _is_mpeg_stream(handle: BinaryIO, offset: int) -> bool:
    """Return whether consistent MPEG audio frames follow one another from here.

    A frame header alone is not enough: random bytes, or UTF-16 text after its
    ``FF FE`` byte order mark, carry a valid-looking header often enough. The
    frame must be followed directly by a second frame with the same version,
    layer and sample rate (a third too when the file starts with ``FF FE``).
    A free-format frame (bitrate index 0) has no computable length, so it is
    never confirmed. The end of the file does not confirm a frame.
    """
    handle.seek(offset)
    first = handle.read(4)
    needed = (
        _FRAMES_TO_CONFIRM_BOM
        if offset == 0 and first.startswith(_UTF16_LE_BOM)
        else _FRAMES_TO_CONFIRM
    )
    key = _mpeg_stream_key(first)
    length = _mpeg_frame_length(first)
    if key is None or not length:
        return False
    position = offset
    for _ in range(needed - 1):
        position += length
        handle.seek(position)
        header = handle.read(4)
        length = _mpeg_frame_length(header)
        if not length or _mpeg_stream_key(header) != key:
            return False
    return True


def _mpeg_stream_key(header: bytes) -> tuple[int, int, int] | None:
    """Return the (version, layer, sample rate index) a stream keeps per frame."""
    if len(header) < 4:
        return None
    return (header[1] >> 3) & 0b11, (header[1] >> 1) & 0b11, (header[2] >> 2) & 0b11


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
    fmt = _wave_chunks(path).get(b"fmt ")
    if fmt is None or len(fmt) < 26 or int.from_bytes(fmt[:2], "little") != 0xFFFE:
        return None
    return int.from_bytes(fmt[24:26], "little")


_WAVE_ADPCM_FORMATS: Final = frozenset({0x0002, 0x0011})
"""MS ADPCM and IMA ADPCM: their ``fmt `` extension holds samples per block."""
_WAVE_CHUNKS_READ: Final = frozenset({b"fmt ", b"fact"})
_WAVE_CHUNK_LIMIT: Final = 1 << 16
"""Largest ``fmt `` or ``fact`` chunk read; real ones are tens of bytes."""


def wave_fact_duration(path: Path) -> float | None:
    """Return the duration a compressed WAV's ``fact`` chunk states, if it holds.

    For a block-based codec (MS ADPCM, IMA ADPCM) the data chunk holds whole
    blocks, so a decoder that counts blocks overstates the duration by the
    unused end of the last block. The ``fact`` chunk gives the real number of
    samples per channel. It is accepted when it is above zero and does not
    exceed what the whole blocks in the ``data`` chunk can hold.

    Args:
        path: A RIFF WAVE file.

    Returns:
        ``fact`` samples divided by the sample rate, or ``None`` when the file
        is not MS or IMA ADPCM, has no usable ``fact`` chunk, or the chunk
        claims more samples than the data holds.
    """
    chunks = _wave_chunks(path)
    fmt = chunks.get(b"fmt ")
    fact = chunks.get(b"fact")
    data_size = _wave_data_size(path)
    if fmt is None or fact is None or len(fmt) < 20 or len(fact) < 4:
        return None
    format_code = int.from_bytes(fmt[0:2], "little")
    sample_rate = int.from_bytes(fmt[4:8], "little")
    block_align = int.from_bytes(fmt[12:14], "little")
    extension_size = int.from_bytes(fmt[16:18], "little")
    samples_per_block = int.from_bytes(fmt[18:20], "little")
    samples = int.from_bytes(fact[0:4], "little")
    if (
        format_code not in _WAVE_ADPCM_FORMATS
        or extension_size < 2
        or not sample_rate
        or not block_align
        or not samples_per_block
        or not data_size
    ):
        return None
    capacity = (data_size // block_align) * samples_per_block
    if not 0 < samples <= capacity:
        return None
    return samples / sample_rate


def _wave_chunks(path: Path) -> dict[bytes, bytes]:
    """Return the bodies of the first ``fmt `` and ``fact`` chunks of a WAV."""
    found: dict[bytes, bytes] = {}
    for chunk_id, _size, body in _walk_wave(path, read=_WAVE_CHUNKS_READ):
        found.setdefault(chunk_id, body)
    return found


def _wave_data_size(path: Path) -> int | None:
    """Return the size the first ``data`` chunk declares, within the file."""
    for chunk_id, size, _body in _walk_wave(path, read=frozenset()):
        if chunk_id == b"data":
            return size
    return None


def _walk_wave(
    path: Path,
    *,
    read: frozenset[bytes],
) -> list[tuple[bytes, int, bytes]]:
    """List a RIFF WAVE file's top-level chunks.

    Args:
        path: The file.
        read: Chunk ids whose bodies are read (up to :data:`_WAVE_CHUNK_LIMIT`).

    Returns:
        ``(id, size, body)`` per chunk, ``body`` empty unless read; a chunk
        that runs past the end of the file has its size cut to what is there.
        Empty when the file is not RIFF WAVE or cannot be read.
    """
    chunks: list[tuple[bytes, int, bytes]] = []
    try:
        with path.open("rb") as handle:
            if handle.read(12)[8:12] != b"WAVE":
                return []
            file_size = handle.seek(0, SEEK_END)
            position = 12
            while position + 8 <= file_size:
                handle.seek(position)
                header = handle.read(8)
                size = min(
                    int.from_bytes(header[4:8], "little"), file_size - position - 8
                )
                body = b""
                if header[:4] in read and size <= _WAVE_CHUNK_LIMIT:
                    body = handle.read(size)
                chunks.append((header[:4], size, body))
                position += 8 + size + (size & 1)
    except OSError:
        return []
    return chunks


_MP4_TRACK_BOX: Final = bytes.fromhex("7472616b")
"""The MP4 track box type, the four ASCII letters t, r, a, k."""
_MP4_CONTAINER_BOXES: Final = frozenset({b"moov", _MP4_TRACK_BOX, b"mdia"})
_MP4_MAX_BOXES: Final = 4096
"""Boxes visited at most, so a malformed file cannot keep the walk going."""


def mp4_audio_track_count(path: Path) -> int | None:
    """Count the audio tracks (``hdlr`` type ``soun``) in an MP4 file.

    Args:
        path: An MP4 file.

    Returns:
        The number of audio tracks, or ``None`` when the box structure cannot
        be walked.
    """
    try:
        with path.open("rb") as handle:
            end = handle.seek(0, SEEK_END)
            budget = [_MP4_MAX_BOXES]
            return _count_audio_tracks(handle, 0, end, budget)
    except (OSError, _MalformedBoxError):
        return None


class _MalformedBoxError(Exception):
    """An MP4 box whose size does not fit its parent."""


def _count_audio_tracks(
    handle: BinaryIO,
    start: int,
    end: int,
    budget: list[int],
) -> int:
    """Count ``soun`` handlers in the boxes between ``start`` and ``end``."""
    count = 0
    position = start
    while position + 8 <= end:
        budget[0] -= 1
        if budget[0] < 0:
            raise _MalformedBoxError
        handle.seek(position)
        header = handle.read(16)
        size = int.from_bytes(header[0:4], "big")
        box_type = header[4:8]
        header_size = 8
        if size == 1:
            size = int.from_bytes(header[8:16], "big")
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            raise _MalformedBoxError
        if box_type in _MP4_CONTAINER_BOXES:
            count += _count_audio_tracks(
                handle,
                position + header_size,
                position + size,
                budget,
            )
        elif box_type == b"hdlr":
            handle.seek(position + header_size + 8)
            count += handle.read(4) == b"soun"
        position += size
    return count
