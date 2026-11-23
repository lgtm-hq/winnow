"""Rule 4 of media-type detection: winnow's own signature table (#393).

Every entry of :data:`SIGNATURES` is a format winnow recognises from its
content: an exact byte pattern at a fixed offset, checked further where the
pattern alone is short or shared (the comment on each entry, or the
docstring of its check in :mod:`winnow.media._header_checks`, cites the
format's specification). Entries are tried in table order and the first
that matches decides. An entry either names the media type, takes it from a
walk over the container's stream declarations
(:mod:`winnow.media._containers`, :mod:`winnow.media._packet_streams`), or
marks the format as non-media; a
non-media match is final, so a PDF named ``.jpg`` is not an image.

After the table come the formats with no fixed header:

* MPEG transport streams, by repeated sync bytes
  (:mod:`winnow.media._mpegts`);
* elementary audio streams (MPEG audio, ADTS AAC, AC-3, E-AC-3, DTS in
  its 16-bit and 14-bit forms of either byte order), by consecutive valid
  frame headers (:mod:`winnow.media._audio_frames`);
* ID3v2 tags, each skipped by its synchsafe size (only the 10-byte headers
  are read), then FLAC or audio frames read once after the last tag. An
  ID3v2 tag followed by neither is only a hint, recorded in
  :attr:`Content.hints`; it decides nothing.

A container walk decides audio only from a complete stream list in which
every stream is recognised; otherwise ffprobe is asked, and without it the
answer is unknown with a signal saying why.

RIFF, RF64, BW64 and IFF ``FORM`` files whose form type is not in the
table are recognised but unknown.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from winnow.media import _format_checks as more
from winnow.media import _header_checks as checks
from winnow.media import _text_formats as text
from winnow.media._audio_frames import (
    MAX_ID3_TAGS,
    FrameLength,
    ac3_frame_length,
    adts_frame_length,
    dts14_be_frame_length,
    dts14_le_frame_length,
    dts_frame_length,
    dts_le_frame_length,
    frames_follow,
    mpeg_audio_frame_length,
    skip_id3v2,
)
from winnow.media._containers import (
    ASF_HEADER,
    StreamList,
    asf_streams,
    avi_streams,
    media_type_of,
    realmedia_streams,
)
from winnow.media._detect_base import (
    BudgetExceededError,
    Content,
    Outcome,
    TypeSignal,
    decide,
    evidence,
)
from winnow.media._header_checks import is_text
from winnow.media._mpegts import inspect_transport_stream
from winnow.media._packet_streams import (
    flv_streams,
    ogg_streams,
    program_stream_streams,
)
from winnow.media._stream_probe import probe_stream_types
from winnow.media._text_formats import TEXT_BASED_CONTAINERS
from winnow.models.media import MediaType

HEAD_BYTES: Final[int] = 64 * 1024
"""Opening bytes rules 3 and 4 read."""

TAIL_BYTES: Final[int] = 32
"""Closing bytes read for trailer signatures (the TGA 2.0 footer)."""

MAX_BYTES_READ: Final[int] = 256 * 1024
"""Bytes rules 3 and 4 may read in total, head and tail included."""

ID3_FRAME_BYTES: Final[int] = 64 * 1024
"""Bytes read after an ID3v2 tag to check the frames that follow it."""

Matcher = Callable[[Content], bool]
Walker = Callable[[Content], StreamList]


@dataclass(frozen=True, slots=True)
class Signature:
    """One entry of the signature table.

    Args:
        container: Format name, reported as the result's container.
        matches: Checks the file's content.
        media_type: Media type of a match, when the format fixes it.
        streams: Walk that lists the container's streams; the media type
            then comes from them.
        demuxer: ffprobe demuxer asked when the walk cannot settle the type.
        non_media: The format is not media; a match is final.
        declares_streams: Whether the container declares its streams before
            its media data. When it does not (FLV, MPEG-PS), ffprobe reads
            only a window, so its audio-only answer after an incomplete walk
            is not taken: video may follow.
    """

    container: str
    matches: Matcher
    media_type: MediaType | None = None
    streams: Walker | None = None
    demuxer: str = ""
    non_media: bool = False
    declares_streams: bool = True


def _starts(*prefixes: bytes) -> Matcher:
    """Return a matcher for any of ``prefixes`` at offset 0.

    Args:
        prefixes: Byte patterns.

    Returns:
        The matcher.
    """
    return lambda content: content.head.startswith(prefixes)


def _form(magic: bytes, *forms: bytes) -> Matcher:
    """Return a matcher for a RIFF or IFF container with a form type.

    Args:
        magic: Container magic at offset 0, such as ``RIFF`` or ``FORM``.
        forms: Accepted form types at offset 8.

    Returns:
        The matcher.
    """
    return lambda content: content.head[:4] == magic and content.head[8:12] in forms


SIGNATURES: Final[tuple[Signature, ...]] = (
    # Non-media first: a match is final whatever the extension says.
    # ISO 32000-1, 7.5.2: the header line "%PDF-1.n".
    Signature("pdf", _starts(b"%PDF-"), non_media=True),
    # PKWARE APPNOTE.TXT 4.3.7 and 4.3.16: local file header, end of central
    # directory (an empty archive), spanning marker.
    # OpenRaster is a ZIP with a fixed first entry: checked before ZIP.
    Signature("openraster", checks.openraster, MediaType.IMAGE),
    Signature(
        "zip", _starts(b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"), non_media=True
    ),
    # Playlists point at media and are not media (the issue's ruling).
    Signature("playlist", more.playlist, non_media=True),
    # Images.
    # ITU-T T.81, B.1.1.3 and B.2.1: SOI then the next marker's FF.
    Signature("jpeg", _starts(b"\xff\xd8\xff"), MediaType.IMAGE),
    # PNG (ISO/IEC 15948), 5.2: the eight-byte signature. APNG shares it.
    Signature("png", _starts(b"\x89PNG\r\n\x1a\n"), MediaType.IMAGE),
    # GIF89a specification, 17: header "GIF87a" or "GIF89a".
    Signature("gif", _starts(b"GIF87a", b"GIF89a"), MediaType.IMAGE),
    # Microsoft BITMAPFILEHEADER and BITMAPINFOHEADER.
    Signature("bmp", checks.bmp, MediaType.IMAGE),
    # TIFF 6.0, section 2: byte order and 42. BigTIFF: 43, offset size 8.
    # TIFF-based RAW (CR2, NEF, ARW, DNG, PEF, SR2, ...) shares the header.
    Signature(
        "tiff",
        _starts(
            b"II*\x00",
            b"MM\x00*",
            b"II+\x00\x08\x00\x00\x00",
            b"MM\x00+\x00\x08\x00\x00",
        ),
        MediaType.IMAGE,
    ),
    # Olympus ORF: "IIRO", "IIRS" or "MMOR" in place of 42 (ExifTool Olympus
    # tags; LibRaw identify.cpp).
    Signature("olympus-orf", _starts(b"IIRO", b"IIRS", b"MMOR"), MediaType.IMAGE),
    # Panasonic RW2/RAW/RWL: "IIU\0" (ExifTool PanasonicRaw tags).
    Signature("panasonic-rw2", _starts(b"IIU\x00"), MediaType.IMAGE),
    # Fujifilm RAF header string (ExifTool FujiFilm RAF; LibRaw).
    Signature("fujifilm-raf", _starts(b"FUJIFILMCCD-RAW "), MediaType.IMAGE),
    # Sigma X3F file header magic (Sigma X3F file format documentation),
    # written in hex because the spell checker rewrites its ASCII spelling.
    Signature("sigma-x3f", _starts(bytes.fromhex("464f5662")), MediaType.IMAGE),
    # Minolta MRW: "\0MRM" block (Dalibor Jelinek, MRW format).
    Signature("minolta-mrw", _starts(b"\x00MRM"), MediaType.IMAGE),
    # ARRI raw: "ARRI" and the byte-order mark 12 34 56 78.
    Signature("arri-ari", _starts(b"ARRI\x12\x34\x56\x78"), MediaType.IMAGE),
    # RFC 9649 (WebP), 2.5: RIFF form type "WEBP".
    Signature("webp", _form(b"RIFF", b"WEBP"), MediaType.IMAGE),
    # Microsoft ICONDIR and ICONDIRENTRY.
    Signature("ico", checks.ico, MediaType.IMAGE),
    # ISO/IEC 15444-1, I.5.1: JP2 signature box; A.4.1 and A.5.1: raw
    # codestream, start-of-codestream then image-and-tile-size marker.
    Signature(
        "jpeg2000",
        _starts(b"\x00\x00\x00\x0cjP  \r\n\x87\n", b"\xff\x4f\xff\x51"),
        MediaType.IMAGE,
    ),
    Signature("netpbm", text.netpbm, MediaType.IMAGE),
    Signature("pcx", checks.pcx, MediaType.IMAGE),
    Signature("sgi", checks.sgi, MediaType.IMAGE),
    Signature("dds", checks.dds, MediaType.IMAGE),
    Signature("icns", checks.icns, MediaType.IMAGE),
    # OpenEXR file layout: magic number 20000630 (little-endian).
    Signature("openexr", _starts(b"\x76\x2f\x31\x01"), MediaType.IMAGE),
    # Sun rasterfile.h: RAS_MAGIC 0x59a66a95.
    Signature("sun-raster", _starts(b"\x59\xa6\x6a\x95"), MediaType.IMAGE),
    Signature("xbm", text.xbm, MediaType.IMAGE),
    # XPM manual (version 3.4), 2: the comment "/* XPM */" first; XPM2 files
    # open with "! XPM2" (same manual, 1.2).
    Signature("xpm", text.xpm, MediaType.IMAGE),
    Signature("eps", text.eps, MediaType.IMAGE),
    Signature("xcf", checks.xcf, MediaType.IMAGE),
    # ITU-T T.832 (JPEG XR), A.7.1: "II", 0xBC, FILE_VERSION_ID 1.
    Signature("jpeg-xr", _starts(b"II\xbc\x01"), MediaType.IMAGE),
    # EA IFF 85 and the ILBM specification: FORM type "ILBM" or "PBM ".
    Signature("ilbm", _form(b"FORM", b"ILBM", b"PBM "), MediaType.IMAGE),
    Signature("fits", checks.fits, MediaType.IMAGE),
    Signature("djvu", checks.djvu, MediaType.IMAGE),
    Signature("psd", more.psd, MediaType.IMAGE),
    Signature("canon-crw", more.canon_crw, MediaType.IMAGE),
    Signature("jpeg-xl", more.jpeg_xl, MediaType.IMAGE),
    Signature("jpeg-xs", more.jpeg_xs, MediaType.IMAGE),
    Signature("dpx", more.dpx, MediaType.IMAGE),
    Signature("dcx", more.dcx, MediaType.IMAGE),
    Signature("niff", more.niff, MediaType.IMAGE),
    Signature("qoi", checks.qoi, MediaType.IMAGE),
    Signature("jng", checks.jng, MediaType.IMAGE),
    Signature("modi", checks.modi, MediaType.IMAGE),
    Signature("xcursor", more.xcursor, MediaType.IMAGE),
    Signature("emf", more.emf, MediaType.IMAGE),
    Signature("quicktime-image", more.quicktime_image, MediaType.IMAGE),
    Signature("kodak-kdc", more.kodak_kdc, MediaType.IMAGE),
    Signature("svg", text.svg, MediaType.IMAGE),
    Signature("tga", checks.tga, MediaType.IMAGE),
    # Audio.
    # Microsoft RIFF WAVE; EBU Tech 3306 (RF64); ITU-R BS.2088 (BW64).
    Signature("wav", _form(b"RIFF", b"WAVE"), MediaType.AUDIO),
    Signature("wav", _form(b"RF64", b"WAVE"), MediaType.AUDIO),
    Signature("wav", _form(b"BW64", b"WAVE"), MediaType.AUDIO),
    Signature("wave64", checks.wave64, MediaType.AUDIO),
    Signature("adif", checks.adif, MediaType.AUDIO),
    # Apple AIFF 1.3 and AIFF-C: IFF FORM type "AIFF" or "AIFC".
    Signature("aiff", _form(b"FORM", b"AIFF", b"AIFC"), MediaType.AUDIO),
    Signature("flac", checks.flac, MediaType.AUDIO),
    Signature("au", checks.au, MediaType.AUDIO),
    Signature("caf", checks.caf, MediaType.AUDIO),
    Signature("wavpack", checks.wavpack, MediaType.AUDIO),
    Signature("tta", checks.tta, MediaType.AUDIO),
    Signature("voc", checks.voc, MediaType.AUDIO),
    # RealAudio 3 to 5 header ".ra" FD (RealAudio file format, Helix DNA).
    Signature("realaudio", _starts(b".ra\xfd"), MediaType.AUDIO),
    Signature("monkeys-audio", checks.monkeys_audio, MediaType.AUDIO),
    Signature("musepack", checks.musepack, MediaType.AUDIO),
    Signature("midi", checks.midi, MediaType.AUDIO),
    Signature("amr", more.amr, MediaType.AUDIO),
    # RFC 3625, 3: RIFF form type "QLCM" (QCELP speech).
    Signature("qcelp", _form(b"RIFF", b"QLCM"), MediaType.AUDIO),
    # EA IFF 8SVX specification: FORM type "8SVX" (8-bit sampled voice).
    Signature("8svx", _form(b"FORM", b"8SVX"), MediaType.AUDIO),
    Signature("mod", more.mod, MediaType.AUDIO),
    Signature("s3m", more.scream_tracker_3, MediaType.AUDIO),
    Signature("stm", more.scream_tracker_2, MediaType.AUDIO),
    Signature("fasttracker", more.fasttracker, MediaType.AUDIO),
    Signature("tracker", more.tracker_magic, MediaType.AUDIO),
    Signature("sid", more.sid, MediaType.AUDIO),
    Signature("psf", more.psf, MediaType.AUDIO),
    Signature("xmf", more.xmf, MediaType.AUDIO),
    # Microsoft RIFF MIDI: RIFF form type "RMID" around a Standard MIDI File.
    Signature("midi", _form(b"RIFF", b"RMID"), MediaType.AUDIO),
    # Containers whose streams decide.
    # Microsoft AVI RIFF File Reference: RIFF form type "AVI ".
    Signature("avi", _form(b"RIFF", b"AVI "), streams=avi_streams, demuxer="avi"),
    # ASF specification 3.1: the Header Object GUID first.
    Signature("asf", _starts(ASF_HEADER), streams=asf_streams, demuxer="asf"),
    Signature(
        "flv",
        checks.flv,
        streams=flv_streams,
        demuxer="flv",
        declares_streams=False,
    ),
    Signature("realmedia", checks.realmedia, streams=realmedia_streams, demuxer="rm"),
    # RFC 3533, 6: capture pattern "OggS" and stream structure version 0.
    Signature("ogg", _starts(b"OggS\x00"), streams=ogg_streams, demuxer="ogg"),
    Signature(
        "mpeg-ps",
        checks.program_stream,
        streams=program_stream_streams,
        demuxer="mpeg",
        declares_streams=False,
    ),
    # Video CD (White Book): MPEG program stream sectors in RIFF form "CDXA".
    Signature("video-cd", _form(b"RIFF", b"CDXA"), MediaType.VIDEO),
    # Video.
    Signature("mpeg-video", checks.mpeg_video, MediaType.VIDEO),
    # YUV4MPEG2 stream header "YUV4MPEG2 " (mjpegtools yuv4mpeg(5)).
    Signature("yuv4mpeg", _starts(b"YUV4MPEG2 "), MediaType.VIDEO),
    Signature("dv", checks.dv, MediaType.VIDEO),
    # MNG 1.0: a multiple-image (animation) format, typed video as MIME does.
    Signature("mng", more.mng, MediaType.VIDEO),
    Signature("bink", more.bink, MediaType.VIDEO),
    Signature("smacker", more.smacker, MediaType.VIDEO),
    Signature("nsv", more.nsv, MediaType.VIDEO),
    Signature("sgi-movie", more.sgi_movie, MediaType.VIDEO),
    # WMF last: its non-placeable header is six bytes of small fields.
    Signature("wmf", more.wmf, MediaType.IMAGE),
)
"""winnow's signature table, tried in order; the first match decides."""

_UNLISTED_FORMS: Final[tuple[tuple[bytes, str], ...]] = (
    (b"RIFF", "riff"),
    (b"RF64", "riff"),
    (b"BW64", "riff"),
    (b"FORM", "iff"),
)

_ELEMENTARY_AUDIO: Final[tuple[tuple[str, FrameLength], ...]] = (
    ("mpeg-audio", mpeg_audio_frame_length),
    ("adts", adts_frame_length),
    ("ac3", ac3_frame_length),
    ("dts", dts_frame_length),
    ("dts", dts_le_frame_length),
    ("dts", dts14_be_frame_length),
    ("dts", dts14_le_frame_length),
)
"""Frame-based audio streams, each recognised by consecutive frames."""


def match_signature(content: Content) -> Outcome | None:
    """Apply rule 4 to the file's content.

    Args:
        content: The file's opening and closing bytes.

    Returns:
        The deciding outcome, or ``None`` when nothing in the table matched.
    """
    plain_text = is_text(content.head, size=content.size)
    for signature in SIGNATURES:
        if plain_text and not _may_be_text(signature):
            continue
        if signature.matches(content):
            return _outcome(signature, content)
    if plain_text:
        return None
    head = content.head
    for magic, container in _UNLISTED_FORMS:
        if head[:4] == magic and len(head) >= 12:
            form = head[8:12].decode("latin-1")
            return decide(container, None, f"unknown unsupported: form type {form!r}")
    transport = inspect_transport_stream(content)
    if transport is not None:
        return transport
    audio = _elementary_audio(head, 0, size=content.size, data_offset=0)
    if audio is not None:
        return audio
    return _after_id3(content)


def _may_be_text(signature: Signature) -> bool:
    """Report whether a signature may match a file that is plain text.

    Non-media signatures and text-based formats may; a binary format whose
    magic happens to be printable (``NSVs``, ``MAC ``, ``IMPM``) may not.

    Args:
        signature: A table entry.

    Returns:
        Whether to try it on plain text.
    """
    return signature.non_media or signature.container in TEXT_BASED_CONTAINERS


def _outcome(signature: Signature, content: Content) -> Outcome:
    """Build the outcome of a table match.

    Args:
        signature: The matching entry.
        content: File content.

    Returns:
        The outcome.
    """
    if signature.non_media:
        return decide(signature.container, None, "non-media signature")
    if signature.streams is None:
        return decide(signature.container, signature.media_type, "signature")
    streams = signature.streams(content)
    names = {
        MediaType.VIDEO.value: MediaType.VIDEO,
        MediaType.AUDIO.value: MediaType.AUDIO,
    }
    found = tuple(
        evidence(
            f"stream={kind or 'unrecognised'}",
            names.get(kind or ""),
            source="track-handler",
        )
        for kind in streams.kinds
    )
    listed = evidence(f"streams={'complete' if streams.complete else 'incomplete'}")
    media_type = media_type_of(streams)
    if media_type is not None:
        reason = f"stream declarations {media_type.value}"
        return decide(
            signature.container, media_type, reason, evidence=(*found, listed)
        )
    unrecognised = None in streams.kinds
    if streams.complete and not unrecognised:
        reason = "unknown unresolved: no audio or video stream established"
        return decide(signature.container, None, reason, evidence=(*found, listed))
    probe = probe_stream_types(content.descriptor, demuxer=signature.demuxer)
    probed = evidence(probe.detail, probe.media_type, source="track-handler")
    media_type = probe.media_type
    why = "stream list incomplete" if not streams.complete else "unrecognised stream"
    windowed = not streams.complete and not signature.declares_streams
    if media_type == MediaType.AUDIO and windowed:
        media_type = None
        reason = (
            "unknown unresolved: ffprobe read only a window and saw audio;"
            " that does not establish that no video follows"
        )
    elif media_type is not None:
        reason = f"stream types {media_type.value} from ffprobe"
    elif not probe.available:
        reason = f"unknown unavailable: {why}, ffprobe needed"
    else:
        reason = "unknown unresolved: no audio or video stream established"
    found = (*found, listed, probed)
    return decide(signature.container, media_type, reason, evidence=found)


def _elementary_audio(
    data: bytes,
    offset: int,
    *,
    size: int,
    data_offset: int,
    found: tuple[TypeSignal, ...] = (),
    reason: str = "consecutive frames",
) -> Outcome | None:
    """Recognise a frame-based audio stream starting at ``offset``.

    Args:
        data: Bytes read, starting at file offset ``data_offset``.
        offset: File offset of the first frame.
        size: File size in bytes.
        data_offset: File offset of ``data[0]``.
        found: Evidence gathered before, such as a skipped ID3v2 tag.
        reason: Reason recorded in the deciding signal.

    Returns:
        An audio outcome, or ``None`` when no stream matches.
    """
    for container, frame_length in _ELEMENTARY_AUDIO:
        if frames_follow(
            data,
            offset,
            size=size,
            frame_length=frame_length,
            data_offset=data_offset,
        ):
            return decide(container, MediaType.AUDIO, reason, evidence=found)
    return None


def _after_id3(content: Content) -> Outcome | None:
    """Skip ID3v2 tags and recognise the audio after them.

    Only each tag's 10-byte header is read to skip it; the bytes after the
    last tag are read once.

    Args:
        content: File content.

    Returns:
        An audio outcome, or ``None`` with a hint recorded when the tag is
        followed by no recognised audio or lies past the read budget.
    """
    offset = 0
    tags = 0
    while tags < MAX_ID3_TAGS and offset < content.size:
        header = _bytes_at(content, offset, _ID3_HEADER)
        if header is None:
            content.hints.append(evidence("hint=id3v2 tag past the read budget"))
            return None
        after = skip_id3v2(header, 0)
        if after is None:
            break
        tags += 1
        offset += after
    if tags == 0:
        return None
    window = min(ID3_FRAME_BYTES, max(content.size - offset, 0))
    data = _bytes_at(content, offset, window)
    if data is None:
        content.hints.append(evidence("hint=id3v2 tag past the read budget"))
        return None
    tag = evidence(f"id3v2 tags={tags} audio_offset={offset}")
    if checks.flac_at(data, 0):
        return decide("flac", MediaType.AUDIO, "after id3v2", evidence=(tag,))
    audio = _elementary_audio(
        data,
        offset,
        size=content.size,
        data_offset=offset,
        found=(tag,),
        reason="consecutive frames after id3v2",
    )
    if audio is None:
        content.hints.append(
            evidence("hint=id3v2 tag without audio frames after it", MediaType.AUDIO),
        )
    return audio


_ID3_HEADER: Final[int] = 10


def _bytes_at(content: Content, offset: int, length: int) -> bytes | None:
    """Return the bytes at ``offset``, from the head when it holds them.

    Args:
        content: File content.
        offset: File offset.
        length: Bytes wanted; fewer come back at the end of the file.

    Returns:
        The bytes, or ``None`` when reading them would pass the budget.
    """
    if offset + length <= len(content.head):
        return content.head[offset : offset + length]
    try:
        return content.reader.read_at(offset, length)
    except BudgetExceededError:
        return None


__all__ = [
    "HEAD_BYTES",
    "ID3_FRAME_BYTES",
    "MAX_BYTES_READ",
    "SIGNATURES",
    "TAIL_BYTES",
    "Signature",
    "match_signature",
]
