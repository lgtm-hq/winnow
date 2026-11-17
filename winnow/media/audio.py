"""Audio metadata and tag extraction.

The container is identified from the file's content, never its name
(:mod:`winnow.media._audio_sniff`). tinytag reads the containers it parses
(MPEG, FLAC, MP4, Ogg, WAVE, ASF, AIFF) from an open file object, so its
extension-based parser choice never applies. ffprobe, when installed, reads
what tinytag cannot: other formats (raw ADTS AAC, AC-3, TrueAudio, WavPack,
...), files tinytag fails on, and fields it leaves unknown or that cannot be
trusted.

Which source is trusted for which field:

- tinytag is used only when the container was established from the content.
  It is given the stream from the offset where the sniffer found it, and the
  parser tinytag then used must be the one for that container (its MIME type
  says which). If they disagree, or an MP4 file holds more than one audio
  track (tinytag mixes fields from several), none of tinytag's values is used.
- ``sample_rate``, ``channels`` and ``bit_depth``: tinytag, from the stream
  header it parsed; ffprobe only fills them when tinytag has none.
- ``codec``: tinytag when its MIME type names the codec; otherwise ffprobe;
  otherwise unknown.
- ``duration_seconds`` and ``bitrate``: tinytag only when it named the codec
  and reported a duration above zero (and, for the bitrate, a bitrate above
  zero). A container whose codec tinytag cannot name (a compressed WAV or
  AIFF-C, for example) or a zero duration (FLAC in Ogg) means tinytag's
  timing is not known to be right, so both come from ffprobe; without
  ffprobe they are unknown (``None``), never tinytag's guess. One exception:
  an MS or IMA ADPCM WAV whose ``fact`` chunk states a sample count that fits
  in its data blocks takes its duration from that count.
- ffprobe: only its first audio stream is read, and only numeric values
  above zero count. A stream without both a sample rate and a channel count
  above zero is no reading. A duration ffprobe says it estimated from the
  bitrate is unknown, never a fact.
- A file whose container cannot be established from its content is read by
  ffprobe alone; without a usable ffprobe reading, :class:`MediaError`.
"""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from loguru import logger
from tinytag import TinyTag, TinyTagException

from winnow.exceptions import MediaError
from winnow.media._audio_sniff import (
    AudioContainer,
    SniffedAudio,
    mp4_audio_track_count,
    sniff_audio,
    sniff_audio_container,
    wave_fact_duration,
    wave_subformat,
)
from winnow.media._coerce import (
    coerce_non_negative_float,
    coerce_non_negative_int,
)
from winnow.media.video import _run
from winnow.models.media import MediaMetadata

if TYPE_CHECKING:
    from typing_extensions import Buffer

_FFPROBE: Final = "ffprobe"
_PROBE_TIMEOUT_SECONDS: Final = 30.0
_FFPROBE_BITRATE_ESTIMATE: Final = "Estimating duration from bitrate"
"""libavformat's warning when it derived a duration from size and bitrate."""

_MIME_CODECS: Final[dict[str, str]] = {
    "audio/mpeg": "mp3",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}
"""Labels for MIME types that name the codec without a codecs parameter."""

_CODEC_TOKENS: Final[dict[str, str]] = {
    "alac": "alac",
    "flac": "flac",
    "opus": "opus",
    "vorbis": "vorbis",
    "speex": "speex",
    "mp3": "mp3",
    "mp4a.69": "mp3",
    "mp4a.6b": "mp3",
    "mp4a.66": "aac",
    "mp4a.67": "aac",
    "mp4a.68": "aac",
    "ac-3": "ac3",
    "ec-3": "eac3",
}
"""Labels for RFC 6381 codecs-parameter tokens, matched case-insensitively."""

_WAV_MIME_TYPES: Final = frozenset(
    {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"},
)
_AIFF_MIME_TYPES: Final = frozenset({"audio/aiff", "audio/x-aiff", "audio/aifc"})
_ASF_MIME_TYPES: Final = frozenset(
    {"application/vnd.ms-asf", "audio/x-ms-wma", "video/x-ms-asf"},
)

_ASF_CODECS: Final[dict[str, str]] = {
    "160": "wmav1",
    "161": "wmav2",
    "162": "wmapro",
    "163": "wmalossless",
}
"""ASF audio format codes (hexadecimal, as tinytag prints them) for WMA."""

_WAVE_EXTENSIBLE: Final = "fffe"
"""WAVE format code whose real codec is in the ``fmt `` subformat GUID."""

_WAV_PCM_FORMAT_CODES: Final = frozenset({"1", "3"})
"""WAVE format codes that are plain PCM: 1 integer PCM, 3 IEEE float."""
_WAV_PCM_SUBFORMATS: Final = frozenset({1, 3})

_AIFF_PCM_COMPRESSION_TYPES: Final = frozenset(
    {"none", "sowt", "twos", "raw ", "fl32", "fl64", "in24", "in32"},
)
"""AIFF-C compression types that are plain PCM, matched case-insensitively."""

_FFPROBE_CODEC_LABELS: Final[dict[str, str]] = {
    "pcm_alaw": "alaw",
    "pcm_mulaw": "mulaw",
}
"""ffprobe codec names that are not plain PCM despite their ``pcm_`` prefix."""

_CONTAINER_MEDIA_TYPES: Final[dict[AudioContainer, str]] = {
    AudioContainer.MPEG: "audio/mpeg",
    AudioContainer.FLAC: "audio/flac",
    AudioContainer.MP4: "audio/mp4",
    AudioContainer.OGG: "audio/ogg",
    AudioContainer.WAVE: "audio/wav",
    AudioContainer.ASF: "application/vnd.ms-asf",
    AudioContainer.AIFF: "audio/aiff",
}
"""The MIME type tinytag's parser for each container sets."""

_TAG_KEY_ALIASES: Final[dict[str, str]] = {
    "date": "year",
    "tracknumber": "track",
    "tdor": "originaldate",
}

_TECHNICAL_FIELDS: Final = frozenset(
    {
        "filename",
        "filesize",
        "mime_type",
        "is_lossless",
        "duration",
        "channels",
        "bitrate",
        "bitdepth",
        "samplerate",
    },
)
"""``TinyTag.as_dict`` keys that describe the stream, not tags."""

_TINYTAG_ATTRIBUTES: Final = frozenset(
    name for name in vars(TinyTag()) if not name.startswith("_")
)
"""tinytag's public attributes; an extra field named like one gets a ``_``."""


def extract_audio_metadata(path: Path) -> MediaMetadata:
    """Extract technical metadata from an audio file.

    The container is identified from the content. tinytag reads the
    containers it parses; ffprobe, when installed, reads everything else and
    supplies fields tinytag leaves unknown or whose values cannot be trusted
    (see the module docstring for which source wins for which field). A field
    no trusted source determines is ``None``.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Metadata populated with duration, bitrate, sample rate, channels, bit
        depth, and a codec label.

    Raises:
        MediaError: If the file is missing, if no backend can read it, or if
            reading it needs ffprobe and ffprobe is not installed.
    """
    if not path.is_file():
        raise MediaError(
            "audio file does not exist",
            operation="extract_audio_metadata",
            file_path=path,
        )

    metadata = _metadata_from_tinytag(path=path)
    if metadata is not None and _is_complete(metadata):
        return metadata

    combined = _combine(
        metadata,
        _metadata_from_ffprobe(path=path),
        established_duration=_established_duration(path, metadata),
    )
    if combined is not None:
        return combined

    raise MediaError(
        _unreadable_reason(path),
        operation="extract_audio_metadata",
        file_path=path,
    )


def read_audio_tags(path: Path) -> dict[str, str]:
    """Read human-readable tags from an audio file.

    Tags come from tinytag: its named fields (``title``, ``artist``,
    ``album``, ``albumartist``, ``genre``, ``year``, ``track``, ``disc``,
    ``composer``, ``comment``, ...) and every extra field it reports. Keys are
    lowercase; ``date`` is aliased to ``year``, ``tracknumber`` to ``track`` and
    the ID3 ``TDOR`` frame to ``originaldate``. WAV files return their RIFF
    ``INFO`` tags. A file whose container tinytag does not parse, or that it
    fails on, gives an empty mapping.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Mapping of lowercase tag name to its stringified value. Multi-valued
        tags are joined with ``", "``.
    """
    return _tags_from_tinytag(path=path)


class _StreamView(io.RawIOBase):
    """A read-only view of an open file that starts at a given offset.

    tinytag seeks to 0 and sniffs from there, so the stream the content
    sniffer found past an ID3v2 tag or zero padding is handed to it as a file
    of its own.

    Args:
        handle: The open file.
        offset: Byte offset in ``handle`` that becomes position 0.
    """

    def __init__(self, handle: BinaryIO, offset: int) -> None:
        super().__init__()
        self._handle = handle
        self._offset = offset
        self._size = max(handle.seek(0, io.SEEK_END) - offset, 0)
        self._position = 0

    def readable(self) -> bool:
        """Return ``True``: the view is readable."""
        return True

    def seekable(self) -> bool:
        """Return ``True``: the view is seekable."""
        return True

    def tell(self) -> int:
        """Return the position within the view."""
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move within the view, relative to its start, position or end.

        Args:
            offset: Distance to move.
            whence: ``SEEK_SET``, ``SEEK_CUR`` or ``SEEK_END``.

        Returns:
            The new position.

        Raises:
            OSError: If the position would be negative.
        """
        origin = {io.SEEK_CUR: self._position, io.SEEK_END: self._size}
        position = origin.get(whence, 0) + offset
        if position < 0:
            raise OSError("negative seek position")
        self._position = position
        return position

    def readinto(self, buffer: Buffer, /) -> int:
        """Read into ``buffer`` from the current position.

        Args:
            buffer: Writable buffer to fill.

        Returns:
            The number of bytes read; 0 at the end of the view.
        """
        target = memoryview(buffer).cast("B")
        self._handle.seek(self._offset + self._position)
        data = self._handle.read(min(len(target), max(self._size - self._position, 0)))
        target[: len(data)] = data
        self._position += len(data)
        return len(data)


@contextmanager
def _open_stream(path: Path, offset: int) -> Iterator[BinaryIO]:
    """Open ``path`` as a file that starts at ``offset``.

    Args:
        path: The audio file.
        offset: Byte offset that becomes position 0.

    Yields:
        A binary file object.
    """
    with path.open("rb") as handle:
        if offset == 0:
            yield handle
            return
        with io.BufferedReader(_StreamView(handle, offset)) as view:
            yield view


def _read_tinytag(path: Path) -> TinyTag | None:
    """Parse a file with tinytag for its tags.

    An MPEG file is read from byte 0, where its ID3v2 tag is; any other
    container from where the sniffer found it (see :func:`_parse_with_tinytag`).
    A result whose parser is known to be the wrong one is dropped.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        The parsed tag, or ``None`` when no container was established, parsing
        fails, or tinytag parsed the file as a different container.
    """
    sniffed = sniff_audio(path)
    if sniffed is None:
        logger.debug("no tinytag container recognised in the content of {}", path)
        return None
    from_start = sniffed.container is AudioContainer.MPEG and sniffed.id3_tag
    tag = _parse_with_tinytag(path, 0 if from_start else sniffed.offset)
    mime_type = getattr(tag, "mime_type", None)
    if tag is None or (mime_type and not _parser_matches(mime_type, sniffed)):
        return None
    return tag


def _read_tinytag_stream(path: Path, sniffed: SniffedAudio | None) -> TinyTag | None:
    """Parse a file's audio stream with tinytag's parser for the sniffed container.

    The file is handed to tinytag from the offset where the sniffer found the
    stream, so tinytag's own magic-byte choice lands on the parser for that
    container. Afterwards the parser tinytag used, which its MIME type names,
    must be the one for the sniffed container: if it is not, or cannot be
    told, tinytag's reading is not used.

    Args:
        path: Filesystem path to the audio file.
        sniffed: What :func:`sniff_audio` established for it.

    Returns:
        The parsed tag, or ``None`` when no container was established, parsing
        fails, or tinytag did not parse it as that container.
    """
    if sniffed is None:
        logger.debug("no tinytag container recognised in the content of {}", path)
        return None
    tag = _parse_with_tinytag(path, sniffed.offset)
    if tag is None:
        return None
    if not _parser_matches(tag.mime_type, sniffed):
        logger.debug(
            "tinytag read {} as {!r}, but its content is {}; not using it",
            path,
            tag.mime_type,
            sniffed.container,
        )
        return None
    return tag


def _parse_with_tinytag(path: Path, offset: int) -> TinyTag | None:
    """Run tinytag on ``path`` as if the file started at ``offset``.

    tinytag is given an open file object and no file name, so its
    extension-based parser choice never applies.

    Args:
        path: Filesystem path to the audio file.
        offset: Where the part tinytag reads starts.

    Returns:
        The parsed tag, or ``None`` when parsing fails.
    """
    try:
        with _open_stream(path, offset) as handle:
            return TinyTag.get(file_obj=handle)
    except (TinyTagException, OSError, ValueError) as exc:
        logger.debug("tinytag could not read {}: {}", path, exc)
        return None


def _parser_matches(mime_type: str | None, sniffed: SniffedAudio) -> bool:
    """Return whether tinytag parsed a file as the container sniffed.

    Args:
        mime_type: tinytag's MIME type for the file.
        sniffed: The container established from the content.

    Returns:
        ``True`` when the MIME type is the one tinytag's parser for that
        container sets.
    """
    if not mime_type:
        return False
    media_type, _codecs = _parse_mime_type(mime_type)
    return media_type == _CONTAINER_MEDIA_TYPES[sniffed.container]


def _metadata_from_tinytag(*, path: Path) -> MediaMetadata | None:
    """Build metadata from tinytag.

    A bitrate of zero is not a bitrate and is left unknown. An MP4 file that
    does not hold exactly one audio track is not read: tinytag takes some
    fields from one track and some from another.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Populated metadata, or ``None`` when tinytag cannot read the file,
        finds no audio stream in it, or cannot read it as a single stream.
    """
    sniffed = sniff_audio(path)
    if sniffed is not None and sniffed.container is AudioContainer.MP4:
        tracks = mp4_audio_track_count(path)
        if tracks != 1:
            logger.debug("{} has {} audio tracks; not using tinytag", path, tracks)
            return None
    tag = _read_tinytag_stream(path, sniffed)
    if tag is None:
        return None

    sample_rate = coerce_non_negative_int(tag.samplerate)
    channels = coerce_non_negative_int(tag.channels)
    if not sample_rate and not channels:
        # No stream was parsed. A real zero-length stream still reports its
        # sample rate and channels, so a duration of 0 alone is not enough.
        logger.debug("tinytag found no audio stream in {}", path)
        return None

    kbps = coerce_non_negative_float(tag.bitrate)
    bitrate = int(kbps * 1000) if kbps else None
    return MediaMetadata(
        duration_seconds=coerce_non_negative_float(tag.duration),
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        bit_depth=coerce_non_negative_int(tag.bitdepth),
        codec=_codec_from_mime_type(
            tag.mime_type,
            subformat=_wave_extensible_subformat(path, tag.mime_type),
        ),
    )


def _wave_extensible_subformat(path: Path, mime_type: str | None) -> int | None:
    """Return the subformat of a ``WAVE_FORMAT_EXTENSIBLE`` file, else ``None``.

    Args:
        path: The audio file.
        mime_type: tinytag's MIME type for it.

    Returns:
        The subformat code when tinytag reported format code ``FFFE``.
    """
    if not mime_type:
        return None
    media_type, codecs = _parse_mime_type(mime_type)
    if media_type not in _WAV_MIME_TYPES or codecs[:1] != [_WAVE_EXTENSIBLE]:
        return None
    return wave_subformat(path)


def _codec_from_mime_type(
    mime_type: str | None,
    *,
    subformat: int | None = None,
) -> str | None:
    """Map tinytag's MIME type and codecs parameter to a codec label.

    Args:
        mime_type: Value such as ``audio/mp4; codecs="mp4a.40.2"``.
        subformat: For a ``WAVE_FORMAT_EXTENSIBLE`` WAV, the subformat code
            read from its ``fmt `` chunk.

    Returns:
        A label such as ``aac``, ``alac``, ``mp3``, ``flac``, ``vorbis``,
        ``opus``, ``wmav2`` or ``pcm``, or ``None`` when the MIME type does not
        identify the codec (for example a WAV or AIFF holding compressed
        audio).
    """
    if not mime_type:
        return None
    media_type, codecs = _parse_mime_type(mime_type)
    if media_type in _WAV_MIME_TYPES:
        return _wave_codec(codecs, subformat)
    if media_type in _AIFF_MIME_TYPES:
        return "pcm" if codecs and codecs[0] in _AIFF_PCM_COMPRESSION_TYPES else None
    if media_type in _ASF_MIME_TYPES:
        return _ASF_CODECS.get(codecs[0]) if codecs else None
    return _codec_from_tokens(codecs) or _MIME_CODECS.get(media_type)


def _codec_from_tokens(codecs: list[str]) -> str | None:
    """Label the first RFC 6381 codecs-parameter token winnow knows.

    Args:
        codecs: Lowercase codec tokens.

    Returns:
        The label, or ``None`` when no token is known.
    """
    for token in codecs:
        if token.startswith("mp4a.40."):
            return "aac"
        label = _CODEC_TOKENS.get(token)
        if label is not None:
            return label
    return None


def _wave_codec(codecs: list[str], subformat: int | None) -> str | None:
    """Label a WAV stream from its format code (and subformat, if extensible).

    Args:
        codecs: Codec tokens from tinytag's MIME type; the first is the
            hexadecimal WAVE format code.
        subformat: The ``WAVE_FORMAT_EXTENSIBLE`` subformat code, if known.

    Returns:
        ``pcm`` for integer or float PCM, otherwise ``None``.
    """
    if not codecs:
        return None
    if codecs[0] in _WAV_PCM_FORMAT_CODES:
        return "pcm"
    if codecs[0] == _WAVE_EXTENSIBLE and subformat in _WAV_PCM_SUBFORMATS:
        return "pcm"
    return None


def _parse_mime_type(mime_type: str) -> tuple[str, list[str]]:
    """Split a MIME type into its lowercase type and codecs-parameter tokens.

    Handles quoted and unquoted ``codecs`` values holding one or several
    comma-separated tokens, in any parameter position.

    Args:
        mime_type: Raw MIME type string.

    Returns:
        The ``type/subtype`` part and the list of codec tokens, both lowercase.
        AIFF compression types keep their inner spaces (``"raw "``).
    """
    media_type, *parameters = mime_type.split(";")
    codecs: list[str] = []
    for parameter in parameters:
        name, separator, value = parameter.partition("=")
        if not separator or name.strip().lower() != "codecs":
            continue
        unquoted = value.strip().strip('"').strip("'")
        codecs.extend(
            token.lower().lstrip() for token in unquoted.split(",") if token.strip()
        )
    return media_type.strip().lower(), codecs


def _timing_is_trusted(metadata: MediaMetadata) -> bool:
    """Return whether tinytag's duration and bitrate can be reported as facts.

    They are trusted only when tinytag named the codec (so it parsed a stream
    it understands) and found a duration above zero.

    Args:
        metadata: Metadata read by tinytag.

    Returns:
        ``True`` when the duration and bitrate may be kept.
    """
    return metadata.codec is not None and bool(metadata.duration_seconds)


def _is_complete(metadata: MediaMetadata) -> bool:
    """Return whether tinytag gave every field ffprobe could fill, all trusted.

    Args:
        metadata: Metadata read by tinytag.

    Returns:
        ``True`` when duration, bitrate, sample rate, channels and codec are
        known and the timing is trusted.
    """
    return _timing_is_trusted(metadata) and None not in (
        metadata.duration_seconds,
        metadata.bitrate,
        metadata.sample_rate,
        metadata.channels,
        metadata.codec,
    )


def _established_duration(
    path: Path,
    metadata: MediaMetadata | None,
) -> float | None:
    """Return a duration the file's header establishes where tinytag's is not trusted.

    Only a WAV whose codec tinytag could not name qualifies: its ``fact``
    chunk, as checked by :func:`wave_fact_duration`.

    Args:
        path: The audio file.
        metadata: Metadata read by tinytag, if any.

    Returns:
        The duration in seconds, or ``None``.
    """
    if metadata is None or metadata.codec is not None:
        return None
    if sniff_audio_container(path) is not AudioContainer.WAVE:
        return None
    return wave_fact_duration(path)


def _combine(
    metadata: MediaMetadata | None,
    probed: MediaMetadata | None,
    *,
    established_duration: float | None = None,
) -> MediaMetadata | None:
    """Merge tinytag's and ffprobe's readings by the trust rule.

    Args:
        metadata: Metadata read by tinytag, if any.
        probed: Metadata read by ffprobe, if any.
        established_duration: A duration the file's own header establishes
            where tinytag's timing is not trusted (see
            :func:`_established_duration`); it wins over ffprobe's.

    Returns:
        The combined metadata, or ``None`` when neither backend read the file.
    """
    if metadata is None:
        return probed
    if not _timing_is_trusted(metadata):
        metadata = metadata.model_copy(
            update={"duration_seconds": established_duration, "bitrate": None},
        )
    if probed is None:
        return metadata
    return _fill_missing(metadata, probed)


def _fill_missing(metadata: MediaMetadata, probed: MediaMetadata) -> MediaMetadata:
    """Fill fields tinytag left unknown with ffprobe's values.

    Args:
        metadata: Metadata read by tinytag; its known values win.
        probed: Metadata read by ffprobe.

    Returns:
        A copy of ``metadata`` with its ``None`` fields filled from ``probed``.
    """
    known = metadata.model_dump(exclude_none=True)
    return probed.model_copy(update=known)


def _unreadable_reason(path: Path) -> str:
    """Say why no backend could read a file.

    Args:
        path: The audio file.

    Returns:
        An error message that names ffprobe when its absence is the reason.
    """
    if shutil.which(_FFPROBE) is not None:
        return "unsupported or corrupt audio file"
    if sniff_audio(path) is None:
        return (
            "the audio format cannot be established from the file's content "
            "without ffprobe, and ffprobe (FFmpeg) is not installed"
        )
    return (
        "tinytag cannot read this audio stream, and ffprobe (FFmpeg), which "
        "might, is not installed"
    )


def _metadata_from_ffprobe(*, path: Path) -> MediaMetadata | None:
    """Build metadata from the first audio stream ffprobe reports.

    ffprobe runs at the ``warning`` level, so that its notice that it
    estimated the duration from the bitrate is seen; such a duration is
    reported as unknown.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Populated metadata, or ``None`` when ffprobe is not installed, fails,
        or finds no audio stream.
    """
    binary = shutil.which(_FFPROBE)
    if binary is None:
        logger.debug("ffprobe not found; cannot complete audio metadata for {}", path)
        return None
    try:
        completed = _run(
            argv=[
                binary,
                "-v",
                "warning",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                # An absolute path never starts with "-" (an option) or
                # "name:" (a protocol), so ffprobe reads it as a file.
                str(path.absolute()),
            ],
            timeout=_PROBE_TIMEOUT_SECONDS,
            operation="extract_audio_metadata",
            path=path,
        )
    except MediaError as exc:
        logger.debug("ffprobe could not run for {}: {}", path, exc)
        return None
    if completed.returncode != 0:
        logger.debug("ffprobe could not read {}: {}", path, completed.stderr.strip())
        return None
    probed = _parse_ffprobe_audio(completed.stdout)
    if probed is not None and _FFPROBE_BITRATE_ESTIMATE in completed.stderr:
        logger.debug("ffprobe estimated the duration of {} from its bitrate", path)
        return probed.model_copy(update={"duration_seconds": None})
    return probed


def _parse_ffprobe_audio(payload: str) -> MediaMetadata | None:
    """Parse ffprobe JSON into audio metadata.

    Only streams with ``codec_type`` ``audio`` are considered, so cover art
    stored as a video stream is ignored. Stream duration and bitrate fall back
    to the container's when the stream does not carry them. A numeric value
    that is absent, zero or negative is unknown, and a stream without both a
    sample rate and a channel count above zero is not a usable reading.

    Args:
        payload: Raw JSON emitted by ffprobe.

    Returns:
        Metadata for the first audio stream, or ``None`` when the payload is
        not JSON, holds no audio stream, or the stream's parameters are
        unusable.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    streams = data.get("streams")
    stream = next(
        (
            candidate
            for candidate in (streams if isinstance(streams, list) else [])
            if isinstance(candidate, dict) and candidate.get("codec_type") == "audio"
        ),
        None,
    )
    if stream is None:
        return None
    container = data.get("format")
    if not isinstance(container, dict):
        container = {}

    sample_rate = _positive_int(stream.get("sample_rate"))
    channels = _positive_int(stream.get("channels"))
    if sample_rate is None or channels is None:
        return None
    duration = _positive_float(stream.get("duration"))
    if duration is None:
        duration = _positive_float(container.get("duration"))
    bitrate = _positive_int(stream.get("bit_rate"))
    if bitrate is None:
        bitrate = _positive_int(container.get("bit_rate"))
    return MediaMetadata(
        duration_seconds=duration,
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        bit_depth=_ffprobe_bit_depth(stream),
        codec=_codec_from_ffprobe(stream.get("codec_name")),
    )


def _positive_int(value: object) -> int | None:
    """Return an ffprobe integer field when it is above zero, else ``None``.

    Args:
        value: The raw field.

    Returns:
        The integer, or ``None``.
    """
    return coerce_non_negative_int(value, via_float=True) or None


def _positive_float(value: object) -> float | None:
    """Return an ffprobe decimal field when it is above zero, else ``None``.

    Args:
        value: The raw field.

    Returns:
        The number, or ``None``.
    """
    return coerce_non_negative_float(value) or None


def _ffprobe_bit_depth(stream: dict[str, object]) -> int | None:
    """Return a stream's bit depth, preferring ``bits_per_raw_sample``.

    ffprobe reports 0 when a codec has no fixed sample size, which is read as
    unknown.

    Args:
        stream: One ffprobe stream object.

    Returns:
        The bit depth, or ``None`` when ffprobe does not know it.
    """
    for field in ("bits_per_raw_sample", "bits_per_sample"):
        depth = coerce_non_negative_int(stream.get(field), via_float=True)
        if depth:
            return depth
    return None


def _codec_from_ffprobe(codec_name: object) -> str | None:
    """Map an ffprobe codec name to winnow's codec label.

    Args:
        codec_name: ffprobe's ``codec_name`` for the stream.

    Returns:
        ``pcm`` for plain PCM (``pcm_s16le``, ``pcm_f32be``, ...), ``alaw`` and
        ``mulaw`` for G.711, otherwise ffprobe's own name; ``None`` if absent.
    """
    if not isinstance(codec_name, str) or not codec_name:
        return None
    label = _FFPROBE_CODEC_LABELS.get(codec_name)
    if label is not None:
        return label
    return "pcm" if codec_name.startswith("pcm_") else codec_name


def _normalize_tag_key(key: str) -> str:
    """Normalize a raw tag key to a canonical lowercase name.

    Args:
        key: Raw tag key from a backend.

    Returns:
        Lowercase key with known aliases mapped to their canonical form.
    """
    lowered = key.lower()
    return _TAG_KEY_ALIASES.get(lowered, lowered)


def _tags_from_tinytag(*, path: Path) -> dict[str, str]:
    """Read tags via tinytag.

    tinytag stores an extra field whose name clashes with one of its own
    attributes under that name with a leading underscore (ffmpeg's WMA
    ``title`` descriptor becomes ``_title`` next to ``title``). Such a field's
    values are merged into the attribute's key instead of appearing as a
    separate ``_``-prefixed tag.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Mapping of tag name to joined string value; empty on failure.
    """
    tag = _read_tinytag(path)
    if tag is None:
        return {}

    collected: dict[str, list[str]] = {}
    for key, value in tag.as_dict().items():
        if key in _TECHNICAL_FIELDS:
            continue
        values = [
            str(item)
            for item in (value if isinstance(value, list) else [value])
            if item not in (None, "")
        ]
        if not values:
            continue
        clashing = key.removeprefix("_")
        if key.startswith("_") and clashing in _TINYTAG_ATTRIBUTES:
            merged = collected.setdefault(_normalize_tag_key(clashing), [])
            merged.extend(item for item in values if item not in merged)
            continue
        collected[_normalize_tag_key(key)] = values
    return {key: ", ".join(values) for key, values in collected.items()}
