"""Bounded reads of image metadata Pillow could not give (#395 PR A1).

These readers are the fallback of :class:`~winnow.media.readers.ExifReader`
for an image Pillow cannot open (a truncated stream, an image above its
pixel limit). They never decode pixels.

- :func:`scan_jpeg` walks a JPEG's marker segments up to the image data and
  returns the EXIF ``APP1`` payload and the IPTC resources of every
  Photoshop ``APP13`` segment.
- :func:`read_tiff_tags` reads the exact bytes of a TIFF's IPTC-NAA tag
  (33723) and its ExifIFD MakerNote (``0x927C``), classic or BigTIFF.
- :func:`iim_datasets` splits an IPTC IIM block into datasets.

Every reader checks each declared length against the bytes that are there,
keeps what it read before damage, and names the damage.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from functools import partial
from typing import BinaryIO, Final, NamedTuple

SCAN_BUDGET: Final[int] = 16 * 1024 * 1024
"""Most bytes a fallback read covers, read or skipped: 16 MiB.

JPEG metadata segments precede the image data and hold at most 64 KiB
each, so 16 MiB is room for 256 full segments, more than the ICC profile,
MPF index and preview segments of camera files need. A TIFF tag read is
held to the same limit. It bounds a crafted file (a long run of fill bytes,
a huge declared length) to a few thousand block reads."""

MAX_SEGMENTS: Final[int] = 1024
"""Most JPEG marker segments walked."""

MAX_GAP: Final[int] = 64 * 1024
"""Most bytes searched for one JPEG marker, fill and stray bytes together."""

MAX_GAPS: Final[int] = 8
"""Most stray-byte gaps a JPEG scan resynchronises over before stopping."""

_BLOCK: Final[int] = 4096
_SOI: Final[bytes] = b"\xff\xd8"
_FILL: Final[int] = 0xFF
_STUFFED: Final[int] = 0x00
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
_IIM_MAX_LENGTH_BYTES: Final[int] = 4

_TIFF_CLASSIC: Final[int] = 42
_TIFF_BIG: Final[int] = 43
_TIFF_ORDERS: Final[dict[bytes, str]] = {b"II": "<", b"MM": ">"}
_TIFF_MAX_ENTRIES: Final[int] = 4096
_TIFF_IPTC: Final[int] = 33723
_TIFF_EXIF_IFD: Final[int] = 0x8769
_TIFF_MAKER_NOTE: Final[int] = 0x927C
_TIFF_TYPE_SIZES: Final[dict[int, int]] = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
    13: 4,
    16: 8,
    17: 8,
    18: 8,
}
"""Bytes per value of each TIFF field type, BigTIFF's 8-byte types included."""


@dataclass(frozen=True, slots=True)
class JpegMetadata:
    """What a JPEG's marker segments hold.

    Args:
        exif: The first EXIF ``APP1`` payload, ``Exif\\0\\0`` header included,
            as far as the file holds it; ``None`` without one.
        iptc: Each IPTC-NAA (``0x0404``) resource, in file order, as far as
            the file holds it.
        problems: Each place the segments are damaged or the scan stopped
            early.
    """

    exif: bytes | None = None
    iptc: tuple[bytes, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TiffTagBytes:
    """The exact bytes of a TIFF's IPTC-NAA tag and ExifIFD MakerNote.

    Args:
        iptc: Tag 33723's bytes, or ``None`` when absent or not recovered.
        maker_note: The MakerNote's bytes, or ``None`` when absent or not
            recovered.
        problems: Why a present tag was not recovered completely.
    """

    iptc: bytes | None = None
    maker_note: bytes | None = None
    problems: tuple[str, ...] = ()


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


class _Budgeted:
    """A file read under :data:`SCAN_BUDGET`.

    Args:
        handle: The file, at its start.
    """

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._size = handle.seek(0, 2)
        handle.seek(0)
        self.left = SCAN_BUDGET
        self.exhausted = False

    def tell(self) -> int:
        """Return the current offset.

        Returns:
            The offset.
        """
        return self._handle.tell()

    def seek(self, offset: int) -> None:
        """Move to an offset already paid for.

        Args:
            offset: The offset.
        """
        self._handle.seek(offset)

    def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes within the budget.

        Args:
            size: The bytes wanted.

        Returns:
            The bytes read; fewer at the end of the file or the budget.
        """
        allowed = min(size, self.left)
        self.exhausted = self.exhausted or allowed < size
        data = self._handle.read(allowed)
        self.left -= len(data)
        return data

    def skip(self, size: int) -> bool:
        """Skip ``size`` bytes within the budget.

        Args:
            size: The bytes to skip.

        Returns:
            ``True`` when the skip stays within the file and the budget.
        """
        if size > self.left:
            self.exhausted = True
            return False
        target = self.tell() + size
        if target > self._size:
            return False
        self.left -= size
        self._handle.seek(target)
        return True


def scan_jpeg(handle: BinaryIO) -> JpegMetadata:
    """Collect the EXIF and IPTC payloads of a JPEG.

    The walk stops at the start of scan, the end of image or the end of the
    file. Stray bytes between segments are skipped up to the next plausible
    marker (at most :data:`MAX_GAPS` gaps of at most :data:`MAX_GAP` bytes)
    and reported. A segment cut short is returned as far as it goes and
    reported, and the walk stops there. A file that ends before the image
    data is not a metadata problem.

    Args:
        handle: The file, at its start.

    Returns:
        The payloads found and the damage seen.
    """
    reader = _Budgeted(handle)
    if reader.read(2) != _SOI:
        return JpegMetadata(problems=("JPEG: no start-of-image marker",))
    scan = _JpegScan()
    for _ in range(MAX_SEGMENTS):
        found = _next_marker(reader, scan.problems)
        if found is None or not scan.accept_gap(*found[1:]):
            break
        code = found[0]
        if code in (_SOS, _EOI):
            break
        if code in _STANDALONE:
            continue
        payload, damage = _segment_payload(reader, code, keep=code in (_APP1, _APP13))
        scan.collect(code, payload)
        if damage is not None:
            scan.problems.append(damage)
            break
    else:
        scan.problems.append(f"JPEG: scan stopped after {MAX_SEGMENTS} segments")
    if reader.exhausted:
        scan.problems.append(f"JPEG: scan stopped after {SCAN_BUDGET} bytes")
    return JpegMetadata(
        exif=scan.exif,
        iptc=tuple(scan.iptc),
        problems=tuple(scan.problems),
    )


@dataclass(slots=True)
class _JpegScan:
    """What a JPEG scan has collected so far.

    Args:
        exif: The first EXIF ``APP1`` payload.
        iptc: The IPTC resources.
        problems: The damage seen.
        gaps: Stray-byte gaps skipped.
    """

    exif: bytes | None = None
    iptc: list[bytes] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    gaps: int = 0

    def accept_gap(self, gap: int, at: int) -> bool:
        """Report stray bytes before a marker.

        Args:
            gap: Stray bytes skipped before the marker's fill run.
            at: Offset of the fill run.

        Returns:
            Whether the scan may go on: ``False`` past :data:`MAX_GAPS`.
        """
        if not gap:
            return True
        self.gaps += 1
        self.problems.append(
            f"JPEG: {gap} stray bytes skipped before the marker at {at}"
        )
        if self.gaps <= MAX_GAPS:
            return True
        self.problems.append(f"JPEG: scan stopped after {MAX_GAPS} gaps")
        return False

    def collect(self, code: int, payload: bytes) -> None:
        """Keep a segment's EXIF or IPTC payload.

        Args:
            code: The marker code.
            payload: The payload, as far as the file holds it.
        """
        if code == _APP1 and self.exif is None and payload.startswith(_EXIF_HEADER):
            self.exif = payload
        elif code == _APP13 and payload.startswith(_PHOTOSHOP_HEADER):
            resources, damage = _photoshop_iptc(payload[len(_PHOTOSHOP_HEADER) :])
            self.iptc.extend(resources)
            self.problems.extend(damage)


def _next_marker(
    reader: _Budgeted,
    problems: list[str],
) -> tuple[int, int, int] | None:
    """Find the next marker code, reading in blocks.

    ``0xFF`` fill before a marker is legal. Any other byte, including a
    stray ``FF 00``, belongs to a gap the caller reports. The search covers
    at most :data:`MAX_GAP` bytes.

    Args:
        reader: The file, just after a segment.
        problems: Receives a problem when the search gives up.

    Returns:
        The marker code with the file just after it, the stray bytes
        skipped before its fill run, and the run's offset; ``None`` at the
        end of the file or the budget, or past :data:`MAX_GAP`.
    """
    start = reader.tell()
    run_start: int | None = None
    while reader.tell() - start < MAX_GAP:
        base = reader.tell()
        block = reader.read(min(_BLOCK, MAX_GAP - (base - start)))
        if not block:
            return None
        found = _marker_in(block, base, run_start)
        if isinstance(found, tuple):
            code, offset, run_start = found
            reader.seek(base + offset + 1)
            return code, run_start - start, run_start
        run_start = found
    problems.append(f"JPEG: no marker within {MAX_GAP} bytes of offset {start}")
    return None


def _marker_in(
    block: bytes,
    base: int,
    run_start: int | None,
) -> tuple[int, int, int] | int | None:
    """Look for a marker in one block.

    Args:
        block: The bytes read.
        base: Their offset in the file.
        run_start: Offset where a run of ``0xFF`` that reached the previous
            block's end began, if any.

    Returns:
        ``(code, offset in block, run start)`` for a marker; otherwise the
        start of a ``0xFF`` run still open at the block's end, or ``None``.
    """
    offset = 0
    while offset < len(block):
        if run_start is None:
            found = block.find(b"\xff", offset)
            if found < 0:
                return None
            run_start, offset = base + found, found + 1
        rest = block[offset:]
        offset += len(rest) - len(rest.lstrip(b"\xff"))
        if offset >= len(block):
            return run_start
        if block[offset] != _STUFFED:
            return block[offset], offset, run_start
        run_start, offset = None, offset + 1
    return run_start


def _segment_payload(
    reader: _Budgeted,
    code: int,
    *,
    keep: bool,
) -> tuple[bytes, str | None]:
    """Read or skip one segment's payload.

    Args:
        reader: The file, just after the marker.
        code: The marker code, for the problem text.
        keep: Whether to return the payload; other segments are skipped.

    Returns:
        The payload as far as the file holds it (empty when skipped), and a
        problem when the segment is damaged or cut short.
    """
    header = reader.read(2)
    if len(header) < 2:
        return b"", f"JPEG: segment 0x{code:02X} has no length"
    (length,) = struct.unpack(">H", header)
    if length < 2:
        return b"", f"JPEG: segment 0x{code:02X} declares length {length}"
    start = reader.tell()
    if not keep:
        if reader.skip(length - 2):
            return b"", None
        return b"", f"JPEG: segment 0x{code:02X} at {start} runs past the end"
    payload = reader.read(length - 2)
    if len(payload) < length - 2:
        return payload, (
            f"JPEG: segment 0x{code:02X} declares {length - 2} bytes,"
            f" only {len(payload)} remain"
        )
    return payload, None


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
    two-byte length and that many bytes. A length with its top bit set is
    IIM's extended form: its low bits give the size (here at most four
    bytes) of the real length that follows. Reading stops at the first
    damage; every dataset before it is kept. Trailing NUL padding is not
    damage.

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
        name = f"{record}:{dataset}"
        span = _iim_span(data, position, name)
        if isinstance(span, str):
            return datasets, span
        start, end = span
        datasets.append(IimDataset(record, dataset, data[start:end]))
        position = end
    return datasets, None


def _iim_span(data: bytes, position: int, name: str) -> tuple[int, int] | str:
    """Locate one dataset's value, reading an extended length if present.

    Args:
        data: The IIM block.
        position: Offset of the dataset's tag byte.
        name: ``record:dataset``, for the problem text.

    Returns:
        The value's start and end offsets, or the damage.
    """
    (length,) = struct.unpack(">H", data[position + 3 : position + 5])
    start = position + _IIM_HEADER
    if length & _IIM_EXTENDED:
        size = length & ~_IIM_EXTENDED
        if not 1 <= size <= _IIM_MAX_LENGTH_BYTES:
            return f"IPTC: dataset {name} declares a {size}-byte extended length"
        if start + size > len(data):
            return f"IPTC: dataset {name} extended length cut short"
        length = int.from_bytes(data[start : start + size], "big")
        start += size
    if start + length > len(data):
        return (
            f"IPTC: dataset {name} declares {length} bytes,"
            f" only {len(data) - start} remain"
        )
    return start, start + length


def read_tiff_tags(handle: BinaryIO) -> TiffTagBytes:
    """Read the exact bytes of a TIFF's IPTC-NAA tag and ExifIFD MakerNote.

    Only IFD0 and the ExifIFD it points to are read, each value with one
    bounded read at its declared offset and size. A value the file does not
    hold completely is not returned and is reported.

    Args:
        handle: The file, at its start.

    Returns:
        The bytes found and why a present tag was not recovered.
    """
    reader = _Budgeted(handle)
    layout = _tiff_layout(reader.read(16))
    if isinstance(layout, str):
        return TiffTagBytes(problems=(layout,))
    endian, big, ifd0 = layout
    problems: list[str] = []
    entries = _tiff_ifd(reader, ifd0, endian, big, problems)
    value = partial(_tiff_value, reader, endian=endian, big=big, problems=problems)
    iptc = value("IPTC-NAA", entries.get(_TIFF_IPTC))
    pointer = value("ExifIFD pointer", entries.get(_TIFF_EXIF_IFD))
    maker_note = None
    if pointer is not None:
        offset = int.from_bytes(pointer, "little" if endian == "<" else "big")
        exif_entries = _tiff_ifd(reader, offset, endian, big, problems)
        maker_note = value("MakerNote", exif_entries.get(_TIFF_MAKER_NOTE))
    return TiffTagBytes(iptc=iptc, maker_note=maker_note, problems=tuple(problems))


def _tiff_layout(header: bytes) -> tuple[str, bool, int] | str:
    """Read a TIFF header.

    Args:
        header: The file's first 16 bytes.

    Returns:
        Byte order (``<`` or ``>``), whether it is BigTIFF, and the offset
        of IFD0; or the damage.
    """
    endian = _TIFF_ORDERS.get(header[:2])
    if endian is None or len(header) < 8:
        return "TIFF: no byte-order mark"
    (magic,) = struct.unpack(f"{endian}H", header[2:4])
    if magic == _TIFF_CLASSIC:
        return endian, False, struct.unpack(f"{endian}I", header[4:8])[0]
    if magic == _TIFF_BIG and len(header) == 16:
        return endian, True, struct.unpack(f"{endian}Q", header[8:16])[0]
    return f"TIFF: unknown version {magic}"


def _tiff_ifd(
    reader: _Budgeted,
    offset: int,
    endian: str,
    big: bool,
    problems: list[str],
) -> dict[int, tuple[int, int, bytes]]:
    """Read one TIFF directory's entries.

    Args:
        reader: The file.
        offset: The directory's offset.
        endian: Byte order.
        big: Whether the file is BigTIFF.
        problems: Receives the damage.

    Returns:
        ``tag -> (type, count, value field)``; empty when unreadable.
    """
    count_format, entry_format = ("Q", "HHQ8s") if big else ("H", "HHI4s")
    count_size = struct.calcsize(count_format)
    entry_size = struct.calcsize(f"{endian}{entry_format}")
    reader.seek(offset)
    head = reader.read(count_size)
    if len(head) < count_size:
        problems.append(f"TIFF: directory at {offset} cut short")
        return {}
    (count,) = struct.unpack(f"{endian}{count_format}", head)
    if count > _TIFF_MAX_ENTRIES:
        problems.append(f"TIFF: directory at {offset} declares {count} entries")
        return {}
    data = reader.read(count * entry_size)
    if len(data) < count * entry_size:
        problems.append(f"TIFF: directory at {offset} cut short")
        return {}
    entries: dict[int, tuple[int, int, bytes]] = {}
    for tag, kind, values, inline in struct.iter_unpack(
        f"{endian}{entry_format}", data
    ):
        entries[tag] = (kind, values, inline)
    return entries


def _tiff_value(
    reader: _Budgeted,
    name: str,
    entry: tuple[int, int, bytes] | None,
    *,
    endian: str,
    big: bool,
    problems: list[str],
) -> bytes | None:
    """Read one TIFF entry's value bytes exactly.

    Args:
        reader: The file.
        name: The tag's name, for the problem text.
        entry: ``(type, count, value field)``, or ``None`` when absent.
        endian: Byte order.
        big: Whether the file is BigTIFF.
        problems: Receives the damage.

    Returns:
        The value's bytes, or ``None`` when absent or not held completely.
    """
    if entry is None:
        return None
    kind, count, field = entry
    width = _TIFF_TYPE_SIZES.get(kind)
    if width is None:
        problems.append(f"TIFF: {name} has unknown type {kind}")
        return None
    size = width * count
    if size <= len(field):
        return field[:size]
    (offset,) = struct.unpack(f"{endian}{'Q' if big else 'I'}", field)
    if size > reader.left:
        problems.append(f"TIFF: {name} of {size} bytes exceeds the read budget")
        return None
    reader.seek(offset)
    data = reader.read(size)
    if len(data) < size:
        problems.append(
            f"TIFF: {name} declares {size} bytes at {offset},"
            f" only {len(data)} remain",
        )
        return None
    return data


__all__ = [
    "MAX_GAP",
    "MAX_GAPS",
    "MAX_SEGMENTS",
    "SCAN_BUDGET",
    "IimDataset",
    "JpegMetadata",
    "TiffTagBytes",
    "iim_datasets",
    "read_tiff_tags",
    "scan_jpeg",
]
