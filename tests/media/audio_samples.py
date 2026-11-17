"""Hand-built ID3 samples for the audio backend tests, written at test time.

``write_id3_sample`` needs no external program: it puts a hand-built ID3v2 tag
in front of the MPEG frames of the committed ``sample.mp3`` fixture, so a test
knows exactly which frames are present. The other audio samples are committed
under ``fixtures/audio`` (see the README there); no test runs ffmpeg.
"""

from __future__ import annotations

from pathlib import Path


def _syncsafe(value: int) -> bytes:
    """Encode an ID3v2 syncsafe integer (7 bits per byte)."""
    return bytes(
        [(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F],
    )


def _id3_frame(frame_id: str, text: str, *, version: int) -> bytes:
    """Encode one Latin-1 text frame for ID3v2.3 or ID3v2.4."""
    payload = b"\x00" + text.encode("latin-1")
    size = _syncsafe(len(payload)) if version == 4 else len(payload).to_bytes(4, "big")
    return frame_id.encode("ascii") + size + b"\x00\x00" + payload


def _mpeg_frames(mp3_path: Path) -> bytes:
    """Return the bytes of ``mp3_path`` after any leading ID3v2 tag."""
    data = mp3_path.read_bytes()
    if data[:3] != b"ID3":
        return data
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    return data[10 + size :]


def write_id3_sample(
    output: Path,
    *,
    mp3_path: Path,
    frames: list[tuple[str, str]],
    version: int,
) -> Path:
    """Write an MP3 whose only tag is an ID3v2 tag with exactly ``frames``.

    Args:
        output: File to write.
        mp3_path: MP3 whose MPEG frames are reused.
        frames: ``(frame id, text)`` pairs, in order.
        version: ID3v2 major version, 3 or 4.

    Returns:
        ``output``.
    """
    body = b"".join(
        _id3_frame(frame_id, text, version=version) for frame_id, text in frames
    )
    header = b"ID3" + bytes([version, 0, 0]) + _syncsafe(len(body))
    output.write_bytes(header + body + _mpeg_frames(mp3_path))
    return output
