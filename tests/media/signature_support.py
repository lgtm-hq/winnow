"""Builders for tiny header-only samples of the rule 4 signature formats."""

from __future__ import annotations

import struct
import uuid

MPEG1_L3_128K_44K = b"\xff\xfb\x90\x00"
"""MPEG-1 layer III, 128 kbit/s, 44.1 kHz, no padding: 417-byte frames."""


def mpeg_frames(count: int = 3, header: bytes = MPEG1_L3_128K_44K) -> bytes:
    """Build consecutive MPEG audio frames.

    Args:
        count: Number of frames.
        header: Four-byte frame header.

    Returns:
        The frames, zero-filled after each header.
    """
    return (header + b"\x00" * (417 - len(header))) * count


def adts_frames(count: int = 3, *, length: int = 64, crc: bool = False) -> bytes:
    """Build consecutive ADTS frames (AAC LC, 44.1 kHz, stereo).

    Args:
        count: Number of frames.
        length: Frame length in bytes, header included.
        crc: Whether the frames carry a CRC (protection_absent = 0).

    Returns:
        The frames.
    """
    second = 0xF0 if crc else 0xF1
    header = bytes(
        [
            0xFF,
            second,
            0x50,
            0x80 | ((length >> 11) & 0x3),
            (length >> 3) & 0xFF,
            ((length & 0x7) << 5) | 0x1F,
            0xFC,
        ],
    )
    return (header + b"\x00" * (length - len(header))) * count


def ac3_frames(count: int = 3) -> bytes:
    """Build consecutive AC-3 frames (48 kHz, 128 kbit/s, 512 bytes each).

    Args:
        count: Number of frames.

    Returns:
        The frames.
    """
    header = b"\x0b\x77\x00\x00" + bytes([0x10, 0x40])
    return (header + b"\x00" * (512 - len(header))) * count


def eac3_frames(count: int = 3) -> bytes:
    """Build consecutive E-AC-3 frames (bsid 16, 256 bytes each).

    Args:
        count: Number of frames.

    Returns:
        The frames.
    """
    frmsiz = 256 // 2 - 1
    header = b"\x0b\x77" + bytes([(frmsiz >> 8) & 0x7, frmsiz & 0xFF, 0x3F, 0x80])
    return (header + b"\x00" * (256 - len(header))) * count


def dts_frames(count: int = 3, *, length: int = 1024) -> bytes:
    """Build consecutive DTS core frames.

    Args:
        count: Number of frames.
        length: Frame length in bytes.

    Returns:
        The frames.
    """
    fsize = length - 1
    header = b"\x7f\xfe\x80\x01" + bytes(
        [0xFC, 0x3C | ((fsize >> 12) & 0x3), (fsize >> 4) & 0xFF, (fsize & 0xF) << 4],
    )
    return (header + b"\x00" * (length - len(header))) * count


def id3v2(size: int, *, version: int = 3, footer: bool = False) -> bytes:
    """Build an ID3v2 tag of ``size`` payload bytes.

    Args:
        size: Tag size after the header (synchsafe-encoded).
        version: Major version.
        footer: Set the footer flag and append a footer (version 4).

    Returns:
        The tag.
    """
    synchsafe = bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    flags = 0x10 if footer else 0
    tag = b"ID3" + bytes([version, 0, flags]) + synchsafe + b"\x00" * size
    if footer:
        tag += b"3DI" + bytes([version, 0, flags]) + synchsafe
    return tag


def riff(magic: bytes, form: bytes, chunks: bytes = b"") -> bytes:
    """Build a RIFF-style container.

    Args:
        magic: ``RIFF``, ``RF64`` or ``BW64``.
        form: Form type.
        chunks: Chunk bytes after the form type.

    Returns:
        The container.
    """
    return magic + struct.pack("<I", 4 + len(chunks)) + form + chunks


def riff_chunk(kind: bytes, body: bytes) -> bytes:
    """Build one RIFF chunk, padded to an even length.

    Args:
        kind: Chunk ID.
        body: Chunk body.

    Returns:
        The chunk.
    """
    return (
        kind + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")
    )


def avi(*stream_types: bytes) -> bytes:
    """Build an AVI with one ``strl/strh`` per stream type.

    Args:
        stream_types: ``fccType`` of each stream, such as ``b"vids"``.

    Returns:
        The file.
    """
    strls = b"".join(
        riff_chunk(b"LIST", b"strl" + riff_chunk(b"strh", kind + b"\x00" * 52))
        for kind in stream_types
    )
    hdrl = riff_chunk(b"LIST", b"hdrl" + riff_chunk(b"avih", b"\x00" * 56) + strls)
    return riff(b"RIFF", b"AVI ", hdrl + riff_chunk(b"LIST", b"movi"))


_ASF_GUIDS = {
    "header": "75b22630-668e-11cf-a6d9-00aa0062ce6c",
    "stream": "b7dc0791-a9b7-11cf-8ee6-00c00c205365",
    "audio": "f8699e40-5b4d-11cf-a8fd-00805f5c442b",
    "video": "bc19efc0-5b4d-11cf-a8fd-00805f5c442b",
    "command": "59dacfc0-59e6-11d0-a3ac-00a0c90348f6",
    "other": "00000000-1111-2222-3333-444444444444",
}


def asf(*stream_kinds: str) -> bytes:
    """Build an ASF Header Object with one Stream Properties per stream.

    Args:
        stream_kinds: ``"audio"``, ``"video"``, ``"command"`` or ``"other"``.

    Returns:
        The file.
    """

    def guid(name: str) -> bytes:
        return uuid.UUID(_ASF_GUIDS[name]).bytes_le

    objects = b""
    for number, kind in enumerate(stream_kinds, start=1):
        # Stream type, error correction type, time offset, two lengths, flags
        # (the stream number), reserved: ASF specification 3.3.
        body = guid(kind) + b"\x00" * 32 + struct.pack("<H", number) + b"\x00" * 4
        objects += guid("stream") + struct.pack("<Q", 24 + len(body)) + body
    size = 30 + len(objects)
    header = (
        guid("header") + struct.pack("<Q", size) + struct.pack("<I", 1) + b"\x01\x02"
    )
    return header + objects


def flv(flags: int, *tag_types: int) -> bytes:
    """Build an FLV header and tags.

    Args:
        flags: Header flags (1 video, 4 audio).
        tag_types: Type of each tag (8 audio, 9 video, 18 script).

    Returns:
        The file.
    """
    data = b"FLV\x01" + bytes([flags]) + struct.pack(">I", 9) + b"\x00" * 4
    for tag_type in tag_types:
        body = b"\x00" * 8
        data += bytes([tag_type]) + len(body).to_bytes(3, "big") + b"\x00" * 7 + body
        data += struct.pack(">I", 11 + len(body))
    return data


def realmedia(*mime_types: bytes) -> bytes:
    """Build a RealMedia file with one ``MDPR`` per MIME type.

    Args:
        mime_types: MIME type of each stream.

    Returns:
        The file.
    """
    data = b".RMF" + struct.pack(">IH", 18, 0) + b"\x00" * 8
    for mime in mime_types:
        body = b"\x00" * 30 + b"\x04name" + bytes([len(mime)]) + mime + b"\x00" * 4
        data += b"MDPR" + struct.pack(">IH", 10 + len(body), 0) + body
    return data + b"DATA" + struct.pack(">IH", 18, 0) + b"\x00" * 8


def program_stream(
    *packets: tuple[int, bytes], video_bound: int | None = None
) -> bytes:
    """Build an MPEG-2 program stream: a pack header then PES packets.

    Args:
        packets: ``(stream_id, payload)`` of each PES packet.
        video_bound: Write a system header with this ``video_bound``;
            ``None`` writes none.

    Returns:
        The stream.
    """
    pack = b"\x00\x00\x01\xba\x44\x00\x04\x00\x04\x01\x01\x89\xc3\xf8"
    data = pack
    if video_bound is not None:
        data += b"\x00\x00\x01\xbb\x00\x06\x80\x00\x01\x04"
        data += bytes([0x20 | video_bound]) + b"\xff"
    for stream_id, payload in packets:
        body = b"\x81\x80\x05" + b"\x21\x00\x01\x00\x01" + payload
        data += (
            b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", len(body)) + body
        )
    return data + b"\x00\x00\x01\xb9"


def ogg_page(first_packet: bytes, *, bos: bool = True, serial: int = 1) -> bytes:
    """Build one Ogg page holding one packet.

    Args:
        first_packet: The packet.
        bos: Whether this is a beginning-of-stream page.
        serial: Stream serial number.

    Returns:
        The page (CRC not computed; detection does not check it).
    """
    lacing = [255] * (len(first_packet) // 255) + [len(first_packet) % 255]
    header = b"OggS\x00" + bytes([0x02 if bos else 0x00]) + b"\x00" * 8
    header += struct.pack("<II", serial, 0) + b"\x00" * 4 + bytes([len(lacing)])
    return header + bytes(lacing) + first_packet


VORBIS_ID = b"\x01vorbis" + b"\x00" * 23
THEORA_ID = b"\x80theora" + b"\x00" * 35
OPUS_HEAD = b"OpusHead\x01\x02" + b"\x00" * 9
SKELETON = b"fishead\x00" + b"\x00" * 56
KATE_ID = b"\x80kate\x00\x00\x00" + b"\x00" * 56
