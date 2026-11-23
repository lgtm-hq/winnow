"""MPEG transport streams for detection rule 4 (#393).

A transport stream (ISO/IEC 13818-1, section 2.4.3.2) is a run of 188-byte
packets, each opening with the sync byte ``0x47``. A BDAV stream (``.m2ts``,
Blu-ray Disc Audio-Visual Basic Format, part 3, section 6.2) puts a four-byte
``TP_extra_header`` before each packet, so its sync bytes sit every 192
bytes from offset 4. Either layout is recognised only with
:data:`SYNC_POINTS` sync bytes in a row, or every packet of a shorter file
and at least two.

A sync pattern identifies a container, not video. The media type comes from
the stream types: the Program Association Table (PID 0) names each
program's PMT PID, and each Program Map Table lists its elementary streams
with a ``stream_type`` (13818-1, table 2-34) and descriptors. Any video
stream gives video; audio streams with no video and nothing unrecognised
give audio. When the tables are missing from the bytes read, or a stream
cannot be classified, ffprobe is asked; without it the answer is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from winnow.media._detect_base import Content, Outcome, TypeSignal, decide, evidence
from winnow.media._stream_probe import probe_stream_types
from winnow.models.media import MediaType

PACKET_BYTES: Final[int] = 188
"""Transport packet length (ISO/IEC 13818-1, 2.4.3.2)."""

BDAV_PACKET_BYTES: Final[int] = 192
"""BDAV source packet length: four-byte header plus a transport packet."""

SYNC_BYTE: Final[int] = 0x47
"""Transport packet sync byte."""

SYNC_POINTS: Final[int] = 5
"""Consecutive sync bytes needed to recognise a transport stream."""

_PAT_PID: Final[int] = 0x0000
_PAT_TABLE: Final[int] = 0x00
_PMT_TABLE: Final[int] = 0x02
_SECTION_HEADER: Final[int] = 3
_CRC_BYTES: Final[int] = 4
_STUFFING: Final[int] = 0xFF
_DEMUXER: Final[str] = "mpegts"

_VIDEO: Final[str] = "video"
_AUDIO: Final[str] = "audio"
_DATA: Final[str] = "data"

STREAM_TYPES: Final[dict[int, str]] = {
    # ISO/IEC 13818-1, table 2-34.
    0x01: _VIDEO,  # MPEG-1 video (ISO/IEC 11172-2)
    0x02: _VIDEO,  # MPEG-2 video (ISO/IEC 13818-2)
    0x03: _AUDIO,  # MPEG-1 audio (ISO/IEC 11172-3)
    0x04: _AUDIO,  # MPEG-2 audio (ISO/IEC 13818-3)
    0x05: _DATA,  # private sections
    0x07: _DATA,  # MHEG
    0x08: _DATA,  # DSM-CC
    0x09: _DATA,  # ITU-T H.222.1
    0x0A: _DATA,  # DSM-CC type A
    0x0B: _DATA,  # DSM-CC type B
    0x0C: _DATA,  # DSM-CC type C
    0x0D: _DATA,  # DSM-CC type D
    0x0E: _DATA,  # auxiliary
    0x0F: _AUDIO,  # AAC with ADTS transport (ISO/IEC 13818-7)
    0x10: _VIDEO,  # MPEG-4 Visual (ISO/IEC 14496-2)
    0x11: _AUDIO,  # MPEG-4 audio with LATM transport (ISO/IEC 14496-3)
    0x14: _DATA,  # DSM-CC synchronized download
    0x15: _DATA,  # metadata in PES packets
    0x16: _DATA,  # metadata in sections
    0x1B: _VIDEO,  # AVC (ITU-T H.264)
    0x1C: _AUDIO,  # MPEG-4 audio without transport syntax
    0x1F: _VIDEO,  # SVC sub-bitstream of AVC
    0x20: _VIDEO,  # MVC sub-bitstream of AVC
    0x24: _VIDEO,  # HEVC (ITU-T H.265)
    0x25: _VIDEO,  # HEVC temporal video subset
    0x33: _VIDEO,  # VVC (ITU-T H.266)
    # ATSC A/52, annex A and annex G.
    0x81: _AUDIO,  # AC-3
    0x87: _AUDIO,  # E-AC-3
    # SMPTE RP 227.
    0xEA: _VIDEO,  # VC-1
}
"""Stream types with one meaning in every system that uses them.

Types whose meaning depends on the system (``0x80`` to ``0x86`` mean
different things in ATSC, SCTE and Blu-ray) are left out: they are
unrecognised and send the file to ffprobe.
"""

PRIVATE_PES: Final[int] = 0x06
"""PES private data; its descriptors say what it carries."""

DESCRIPTOR_KINDS: Final[dict[int, str]] = {
    # ETSI EN 300 468, section 6.1, table 12.
    0x45: _DATA,  # VBI data
    0x46: _DATA,  # VBI teletext
    0x56: _DATA,  # teletext
    0x59: _DATA,  # subtitling
    0x6A: _AUDIO,  # AC-3
    0x7A: _AUDIO,  # enhanced AC-3
    0x7B: _AUDIO,  # DTS
    0x7C: _AUDIO,  # AAC
}
"""Descriptor tags that classify a private PES stream."""

_REGISTRATION_DESCRIPTOR: Final[int] = 0x05
REGISTERED_FORMATS: Final[dict[bytes, str]] = {
    # SMPTE registration authority format identifiers.
    b"AC-3": _AUDIO,
    b"EAC3": _AUDIO,
    b"DTS1": _AUDIO,
    b"DTS2": _AUDIO,
    b"DTS3": _AUDIO,
    b"Opus": _AUDIO,  # Opus in MPEG-2 TS (Xiph OpusTS mapping)
    b"BSSD": _AUDIO,  # SMPTE 302M AES3 audio
}
"""Registration-descriptor format identifiers that classify a stream."""


Program = tuple[int, int]
"""A program: its ``program_number`` and its PMT PID."""


@dataclass(slots=True)
class _Tables:
    """Program tables assembled from the packets read.

    Args:
        programs: Programs the PAT names, as ``(program_number, PMT PID)``;
            several programs may share one PMT PID.
        pmts: Streams of each program whose PMT has been read, as
            ``(stream_type, kind)``.
        buffers: Partly assembled sections by PID.
        pat_sections: PAT ``section_number`` values read.
        pat_last: The PAT's ``last_section_number``, once a section is read.
    """

    programs: set[Program] = field(default_factory=set)
    pmts: dict[Program, list[tuple[int, str | None]]] = field(default_factory=dict)
    buffers: dict[int, bytes] = field(default_factory=dict)
    pat_sections: set[int] = field(default_factory=set)
    pat_last: int | None = None

    @property
    def pids(self) -> set[int]:
        """Return the PMT PIDs the PAT names.

        Returns:
            The PIDs.
        """
        return {pid for _, pid in self.programs}

    @property
    def streams(self) -> list[tuple[int, str | None]]:
        """Return the streams of the PAT's programs whose PMT was read.

        Returns:
            ``(stream_type, kind)`` of every elementary stream found.
        """
        return [
            stream
            for program in sorted(self.programs)
            for stream in self.pmts.get(program, [])
        ]

    @property
    def pat_read(self) -> bool:
        """Report whether every section of the PAT has been read.

        Returns:
            ``True`` once sections 0 to ``last_section_number`` are read.
        """
        if self.pat_last is None:
            return False
        return set(range(self.pat_last + 1)) <= self.pat_sections

    @property
    def complete(self) -> bool:
        """Report whether the PAT and the PMT of every program were read.

        Returns:
            ``True`` once all program tables are known.
        """
        return self.pat_read and bool(self.programs) and self.programs <= set(self.pmts)


def layout(head: bytes, size: int) -> tuple[int, int] | None:
    """Find the transport packet layout from repeated sync bytes.

    Args:
        head: The file's opening bytes.
        size: File size in bytes.

    Returns:
        ``(first sync offset, packet stride)`` for a transport stream or a
        BDAV stream, or ``None``.
    """
    for start, stride in ((0, PACKET_BYTES), (4, BDAV_PACKET_BYTES)):
        whole = (size - start) // stride
        wanted = min(SYNC_POINTS, whole)
        if wanted < 2 or start + (wanted - 1) * stride >= len(head):
            continue
        if all(head[start + index * stride] == SYNC_BYTE for index in range(wanted)):
            return start, stride
    return None


def inspect_transport_stream(content: Content) -> Outcome | None:
    """Recognise a transport stream and establish its media type.

    Any video stream in the PMTs read gives video, even before every table
    is read. Audio needs every program's PMT, with every stream recognised.

    Args:
        content: The file's opening bytes and open descriptor.

    Returns:
        The outcome, or ``None`` when the file is not a transport stream.
    """
    found = layout(content.head, content.size)
    if found is None:
        return None
    start, stride = found
    container = "mpeg-ts" if stride == PACKET_BYTES else "m2ts"
    sync = evidence(f"sync=0x47 every {stride} bytes from offset {start}")
    tables = _read_tables(content.head, start=start, stride=stride)
    signals: tuple[TypeSignal, ...] = (sync, *_stream_signals(tables))
    kinds = [kind for _, kind in tables.streams]
    if _VIDEO in kinds:
        return decide(container, MediaType.VIDEO, "pmt video stream", evidence=signals)
    recognised = bool(kinds) and None not in kinds
    if tables.complete and recognised and _AUDIO in kinds:
        return decide(
            container,
            MediaType.AUDIO,
            "pmt audio streams without video",
            evidence=signals,
        )
    if tables.complete and recognised:
        return decide(
            container,
            None,
            "unknown unresolved: pmt lists no audio or video stream",
            evidence=signals,
        )
    if not tables.complete:
        why = "program tables incomplete"
    elif kinds:
        why = "unrecognised stream type"
    else:
        why = "pmt lists no streams"
    probe = probe_stream_types(content.descriptor, demuxer=_DEMUXER)
    probed = (
        *signals,
        evidence(probe.detail, probe.media_type, source="track-handler"),
    )
    if probe.media_type is not None:
        reason = f"stream types {probe.media_type.value} from ffprobe"
    elif not probe.available:
        reason = f"unknown unavailable: {why}, ffprobe needed"
    else:
        reason = "unknown unresolved: no audio or video stream established"
    return decide(container, probe.media_type, reason, evidence=probed)


def _stream_signals(tables: _Tables) -> tuple[TypeSignal, ...]:
    """Describe each elementary stream the PMTs list.

    Args:
        tables: Assembled tables.

    Returns:
        One track signal per stream.
    """
    kinds = {_VIDEO: MediaType.VIDEO, _AUDIO: MediaType.AUDIO}
    return tuple(
        evidence(
            f"pmt stream_type=0x{stream_type:02x} kind={kind or 'unrecognised'}",
            kinds.get(kind or ""),
            source="track-handler",
        )
        for stream_type, kind in tables.streams
    )


def _read_tables(head: bytes, *, start: int, stride: int) -> _Tables:
    """Assemble the PAT and PMTs from the packets in ``head``.

    Args:
        head: The file's opening bytes.
        start: Offset of the first sync byte.
        stride: Packet stride.

    Returns:
        The tables found; possibly incomplete.
    """
    tables = _Tables()
    for offset in range(start, len(head) - PACKET_BYTES + 1, stride):
        packet = head[offset : offset + PACKET_BYTES]
        if packet[0] != SYNC_BYTE:
            break
        _feed_packet(tables, packet)
        if tables.complete:
            break
    return tables


def _feed_packet(tables: _Tables, packet: bytes) -> None:
    """Add one transport packet's payload to the sections being assembled.

    Packets with ``transport_error_indicator`` set, or scrambled
    (``transport_scrambling_control`` not 0), are skipped (13818-1,
    2.4.3.3). When a section starts mid-packet, the bytes before
    ``pointer_field`` finish the previous section first; more than one
    section may follow in the same packet.

    Args:
        tables: Tables being assembled.
        packet: One 188-byte transport packet.
    """
    if packet[1] & 0x80 or packet[3] >> 6:
        return
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    if pid != _PAT_PID and pid not in tables.pids:
        return
    unit_start = bool(packet[1] & 0x40)
    control = (packet[3] >> 4) & 0x3
    payload_at = 4
    if control in {2, 3}:
        payload_at = 5 + packet[4]
    if control in {0, 2} or payload_at >= PACKET_BYTES:
        return
    payload = packet[payload_at:]
    if not unit_start:
        if pid in tables.buffers:
            tables.buffers[pid] += payload
            _drain(tables, pid)
        return
    pointer = payload[0]
    if pid in tables.buffers:
        tables.buffers[pid] += payload[1 : 1 + pointer]
        _drain(tables, pid)
    tables.buffers[pid] = payload[1 + pointer :]
    _drain(tables, pid)


def _drain(tables: _Tables, pid: int) -> None:
    """Handle every whole section waiting in a PID's buffer.

    Args:
        tables: Tables being assembled.
        pid: The PID whose buffer to drain.
    """
    while True:
        section = tables.buffers.get(pid, b"")
        if len(section) < _SECTION_HEADER:
            return
        if section[0] == _STUFFING:
            del tables.buffers[pid]
            return
        length = _SECTION_HEADER + (((section[1] & 0x0F) << 8) | section[2])
        if len(section) < length:
            return
        tables.buffers[pid] = section[length:]
        _handle_section(tables, pid, section[:length])


def _handle_section(tables: _Tables, pid: int, section: bytes) -> None:
    """Read one whole PAT or PMT section.

    Sections with ``current_next_indicator`` 0 describe a future table and
    are ignored (13818-1, 2.4.4.5).

    Args:
        tables: Tables being assembled.
        pid: The PID it arrived on.
        section: The section, CRC included.
    """
    if len(section) < 8 + _CRC_BYTES or not section[5] & 0x01:
        return
    if pid == _PAT_PID and section[0] == _PAT_TABLE:
        _read_pat(tables, section)
    elif section[0] == _PMT_TABLE:
        program = ((section[3] << 8) | section[4], pid)
        if program in tables.programs and program not in tables.pmts:
            tables.pmts[program] = _read_pmt(section)


def _read_pat(tables: _Tables, section: bytes) -> None:
    """Read the programs from a PAT section (13818-1, 2.4.4.3).

    A PAT may span several sections; each records its ``section_number``
    and the ``last_section_number``, so the PAT counts as read only once
    every section is.

    Args:
        tables: Tables being assembled.
        section: The whole section, CRC included.
    """
    tables.pat_sections.add(section[6])
    tables.pat_last = section[7]
    for index in range(8, len(section) - _CRC_BYTES - 3, 4):
        program = (section[index] << 8) | section[index + 1]
        pid = ((section[index + 2] & 0x1F) << 8) | section[index + 3]
        if program != 0:
            tables.programs.add((program, pid))


def _read_pmt(section: bytes) -> list[tuple[int, str | None]]:
    """Read the elementary streams of a PMT section (13818-1, 2.4.4.8).

    Args:
        section: The whole section, CRC included.

    Returns:
        ``(stream_type, kind)`` of each stream.
    """
    streams: list[tuple[int, str | None]] = []
    end = len(section) - _CRC_BYTES
    if end < 12:
        return streams
    index = 12 + (((section[10] & 0x0F) << 8) | section[11])
    while index + 5 <= end:
        stream_type = section[index]
        info = ((section[index + 3] & 0x0F) << 8) | section[index + 4]
        descriptors = section[index + 5 : min(index + 5 + info, end)]
        streams.append((stream_type, classify_stream(stream_type, descriptors)))
        index += 5 + info
    return streams


def classify_stream(stream_type: int, descriptors: bytes) -> str | None:
    """Classify one elementary stream.

    Args:
        stream_type: The PMT ``stream_type``.
        descriptors: The stream's descriptor loop.

    Returns:
        ``"video"``, ``"audio"``, ``"data"``, or ``None`` when unrecognised.
    """
    known = STREAM_TYPES.get(stream_type)
    if known is not None or stream_type != PRIVATE_PES:
        return known
    index = 0
    while index + 2 <= len(descriptors):
        tag = descriptors[index]
        length = descriptors[index + 1]
        body = descriptors[index + 2 : index + 2 + length]
        if tag == _REGISTRATION_DESCRIPTOR and body[:4] in REGISTERED_FORMATS:
            return REGISTERED_FORMATS[body[:4]]
        if tag in DESCRIPTOR_KINDS:
            return DESCRIPTOR_KINDS[tag]
        index += 2 + length
    return None


__all__ = [
    "BDAV_PACKET_BYTES",
    "DESCRIPTOR_KINDS",
    "PACKET_BYTES",
    "PRIVATE_PES",
    "REGISTERED_FORMATS",
    "STREAM_TYPES",
    "SYNC_BYTE",
    "SYNC_POINTS",
    "classify_stream",
    "inspect_transport_stream",
    "layout",
]
