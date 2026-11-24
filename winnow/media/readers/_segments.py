"""Bounded reads of JPEG metadata segments and IPTC IIM datasets (#395 PR A1).

:func:`scan_jpeg` walks a JPEG's marker segments up to the image data and
returns the EXIF ``APP1`` payload and the IPTC resources of every Photoshop
``APP13`` segment, without decoding pixels: a file Pillow refuses (a
truncated stream, an image above its pixel limit) keeps its metadata.
:func:`iim_datasets` splits an IPTC IIM block into datasets, checking every
declared length against the bytes that are there.

Bounds: at most :data:`MAX_SEGMENTS` segments are walked; a segment holds at
most 65,533 bytes by the JPEG format, and only ``APP1`` and ``APP13``
payloads are kept, so memory is bounded too.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Final, NamedTuple

MAX_SEGMENTS: Final[int] = 1024
"""Most marker segments walked before the scan stops."""

_SOI: Final[bytes] = b"\xff\xd8"
_MARKER: Final[int] = 0xFF
_APP1: Final[int] = 0xE1
_APP13: Final[int] = 0xED
_SOS: Final[int] = 0xDA
_EOI: Final[int] = 0xD9
_STANDALONE: Final[frozenset[int]] = frozenset({0x01, *range(0xD0, 0xD8)})
_EXIF_HEADER: Final[bytes] = b"Exif\x00\x00"
_PHOTOSHOP_HEADER: Final[bytes] = b"Photoshop 3.0\x00"
_RESOURCE_SIGNATURE: Final[bytes] = b"8BIM"
_IPTC_RESOURCE: Final[int] = 0x0404
_RESOURCE_HEADER: Final[int] = 6
_IIM_TAG: Final[int] = 0x1C
_IIM_HEADER: Final[int] = 5
_IIM_EXTENDED: Final[int] = 0x8000


@dataclass(frozen=True, slots=True)
class JpegMetadata:
    """What a JPEG's marker segments hold.

    Args:
        exif: The first EXIF ``APP1`` payload, ``Exif\\0\\0`` header included,
            as Pillow uses it; ``None`` without one.
        iptc: Each IPTC-NAA (``0x0404``) resource, in file order.
        problems: Each place the segments are damaged.
    """

    exif: bytes | None = None
    iptc: tuple[bytes, ...] = ()
    problems: tuple[str, ...] = field(default=())


class IimDataset(NamedTuple):
    """One IPTC IIM dataset.

    Args:
        record: The record number, 2 for application records.
        dataset: The dataset number, such as 55 for ``DateCreated``.
        value: Its bytes, exactly as stored.
    """

    record: int
    dataset: int
    value: bytes


def scan_jpeg(handle: BinaryIO) -> JpegMetadata:
    """Collect the EXIF and IPTC payloads of a JPEG.

    The walk stops at the start of scan, the end of image, the end of the
    file or :data:`MAX_SEGMENTS`. A file that ends before the image data is
    not a metadata problem; a segment cut short, a missing marker or a
    length below 2 is.

    Args:
        handle: The file, at its start.

    Returns:
        The payloads found and the damage seen.
    """
    if handle.read(2) != _SOI:
        return JpegMetadata(problems=("JPEG: no start-of-image marker",))
    exif: bytes | None = None
    iptc: list[bytes] = []
    problems: list[str] = []
    for _ in range(MAX_SEGMENTS):
        code = _next_marker(handle, problems)
        if code is None or code in (_SOS, _EOI):
            break
        if code in _STANDALONE:
            continue
        payload = _segment_payload(handle, code, keep=code in (_APP1, _APP13))
        if isinstance(payload, str):
            problems.append(payload)
            break
        if code == _APP1 and exif is None and payload.startswith(_EXIF_HEADER):
            exif = payload
        elif code == _APP13 and payload.startswith(_PHOTOSHOP_HEADER):
            resources, damage = _photoshop_iptc(payload[len(_PHOTOSHOP_HEADER) :])
            iptc.extend(resources)
            problems.extend(damage)
    return JpegMetadata(exif=exif, iptc=tuple(iptc), problems=tuple(problems))


def _next_marker(handle: BinaryIO, problems: list[str]) -> int | None:
    """Read the next marker code, skipping fill bytes.

    Args:
        handle: The file, at a marker.
        problems: Receives a problem when no marker is there.

    Returns:
        The marker code, or ``None`` at the end of the file or on damage.
    """
    first = handle.read(1)
    if not first:
        return None
    if first[0] != _MARKER:
        problems.append(f"JPEG: no marker at offset {handle.tell() - 1}")
        return None
    code = handle.read(1)
    while code and code[0] == _MARKER:
        code = handle.read(1)
    return code[0] if code else None


def _segment_payload(handle: BinaryIO, code: int, *, keep: bool) -> bytes | str:
    """Read or skip one segment's payload.

    Args:
        handle: The file, just after the marker.
        code: The marker code, for the problem text.
        keep: Whether to return the payload; other segments are skipped.

    Returns:
        The payload (empty when skipped), or a problem when the segment is
        damaged or cut short.
    """
    header = handle.read(2)
    if len(header) < 2:
        return f"JPEG: segment 0x{code:02X} has no length"
    (length,) = struct.unpack(">H", header)
    if length < 2:
        return f"JPEG: segment 0x{code:02X} declares length {length}"
    if not keep:
        start = handle.tell()
        end = handle.seek(length - 2, 1)
        size = handle.seek(0, 2)
        if end > size:
            return f"JPEG: segment 0x{code:02X} at {start} runs past the end"
        handle.seek(end)
        return b""
    payload = handle.read(length - 2)
    if len(payload) < length - 2:
        return (
            f"JPEG: segment 0x{code:02X} declares {length - 2} bytes,"
            f" only {len(payload)} remain"
        )
    return payload


def _photoshop_iptc(data: bytes) -> tuple[list[bytes], list[str]]:
    """Collect the IPTC-NAA resources of a Photoshop ``APP13`` payload.

    Args:
        data: The resource blocks after the ``Photoshop 3.0`` header.

    Returns:
        Each IPTC resource's data in order, and the damage seen; a resource
        cut short is returned as far as it goes, with a problem.
    """
    resources: list[bytes] = []
    position = 0
    while position < len(data):
        if data[position : position + 4] != _RESOURCE_SIGNATURE:
            if data[position:].strip(b"\x00"):
                return resources, [f"APP13: no resource signature at {position}"]
            break
        header_end = position + _RESOURCE_HEADER
        if header_end >= len(data):
            return resources, [f"APP13: resource header cut short at {position}"]
        (resource,) = struct.unpack(">H", data[position + 4 : header_end])
        name_length = data[header_end] + 1
        size_at = header_end + name_length + (name_length & 1)
        if size_at + 4 > len(data):
            return resources, [f"APP13: resource 0x{resource:04X} size cut short"]
        (size,) = struct.unpack(">I", data[size_at : size_at + 4])
        start = size_at + 4
        body = data[start : start + size]
        if resource == _IPTC_RESOURCE:
            resources.append(body)
        if start + size > len(data):
            return resources, [
                f"APP13: resource 0x{resource:04X} declares {size} bytes,"
                f" only {len(body)} remain",
            ]
        position = start + size + (size & 1)
    return resources, []


def iim_datasets(data: bytes) -> tuple[list[IimDataset], str | None]:
    """Split an IPTC IIM block into datasets.

    Each dataset is a ``0x1C`` tag byte, its record and dataset numbers, a
    two-byte length and that many bytes. Reading stops at the first damage;
    every dataset before it is kept. Trailing NUL padding is not damage.

    Args:
        data: The IIM block.

    Returns:
        The complete datasets in order, and the damage seen, if any.
    """
    datasets: list[IimDataset] = []
    position = 0
    while position < len(data):
        if data[position] != _IIM_TAG:
            if data[position:].strip(b"\x00"):
                return datasets, f"IPTC: no dataset tag at offset {position}"
            break
        if position + _IIM_HEADER > len(data):
            return datasets, f"IPTC: dataset header cut short at offset {position}"
        record, dataset = data[position + 1], data[position + 2]
        (length,) = struct.unpack(">H", data[position + 3 : position + 5])
        name = f"{record}:{dataset}"
        if length & _IIM_EXTENDED:
            return datasets, f"IPTC: dataset {name} uses an extended length"
        start = position + _IIM_HEADER
        if start + length > len(data):
            return datasets, (
                f"IPTC: dataset {name} declares {length} bytes,"
                f" only {len(data) - start} remain"
            )
        datasets.append(IimDataset(record, dataset, data[start : start + length]))
        position = start + length
    return datasets, None


__all__ = ["MAX_SEGMENTS", "IimDataset", "JpegMetadata", "iim_datasets", "scan_jpeg"]
