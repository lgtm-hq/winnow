"""Audio metadata and tag extraction.

Uses :mod:`mutagen` as the primary backend and falls back to :mod:`tinytag`
when mutagen cannot open a file or yields no usable information. Together these
cover MP3, FLAC, WAV, AAC/MP4, and OGG containers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from loguru import logger
from mutagen import File as mutagen_open
from mutagen import MutagenError
from tinytag import TinyTag, TinyTagException

from winnow.exceptions import MediaError
from winnow.models.media import MediaMetadata

_MUTAGEN_CODECS: Final[dict[str, str]] = {
    "MP3": "mp3",
    "FLAC": "flac",
    "OggVorbis": "vorbis",
    "OggOpus": "opus",
    "OggFLAC": "flac",
    "WAVE": "pcm",
    "AIFF": "pcm",
    "MP4": "aac",
    "AAC": "aac",
}


def extract_audio_metadata(path: Path) -> MediaMetadata:
    """Extract technical metadata from an audio file.

    Tries mutagen first and falls back to tinytag when mutagen cannot read the
    file. Missing individual fields are left as ``None`` rather than failing.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Metadata populated with duration, bitrate, sample rate, channels, and a
        best-effort codec label.

    Raises:
        MediaError: If the file is missing or neither backend can read it.
    """
    if not path.is_file():
        raise MediaError(
            "audio file does not exist",
            operation="extract_audio_metadata",
            file_path=path,
        )

    metadata = _metadata_from_mutagen(path=path)
    if metadata is not None:
        return metadata

    metadata = _metadata_from_tinytag(path=path)
    if metadata is not None:
        return metadata

    raise MediaError(
        "unsupported or corrupt audio file",
        operation="extract_audio_metadata",
        file_path=path,
    )


def read_audio_tags(path: Path) -> dict[str, str]:
    """Read human-readable tags from an audio file.

    mutagen is queried in "easy" mode so tag keys are normalized (``title``,
    ``artist``, ``album``, …) across formats. tinytag is used as a fallback.
    Failures degrade to an empty mapping.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Mapping of lowercase tag name to its stringified value. Multi-valued
        tags are joined with ``", "``.
    """
    tags = _tags_from_mutagen(path=path)
    if tags:
        return tags
    return _tags_from_tinytag(path=path)


def _metadata_from_mutagen(*, path: Path) -> MediaMetadata | None:
    """Build metadata from mutagen stream info.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Populated metadata, or ``None`` when mutagen cannot read the file.
    """
    try:
        audio = mutagen_open(path)
    except (MutagenError, OSError, ValueError) as exc:
        logger.debug("mutagen could not read {}: {}", path, exc)
        return None
    if audio is None or audio.info is None:
        return None

    info = audio.info
    return MediaMetadata(
        duration_seconds=_non_negative_float(getattr(info, "length", None)),
        bitrate=_non_negative_int(getattr(info, "bitrate", None)),
        sample_rate=_non_negative_int(getattr(info, "sample_rate", None)),
        channels=_non_negative_int(getattr(info, "channels", None)),
        codec=_MUTAGEN_CODECS.get(type(audio).__name__),
    )


def _metadata_from_tinytag(*, path: Path) -> MediaMetadata | None:
    """Build metadata from tinytag as a fallback backend.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Populated metadata, or ``None`` when tinytag cannot read the file.
    """
    try:
        tag = TinyTag.get(path)
    except (TinyTagException, OSError, ValueError) as exc:
        logger.debug("tinytag could not read {}: {}", path, exc)
        return None

    kbps = _non_negative_float(tag.bitrate)
    bitrate = int(kbps * 1000) if kbps is not None else None
    return MediaMetadata(
        duration_seconds=_non_negative_float(tag.duration),
        bitrate=bitrate,
        sample_rate=_non_negative_int(tag.samplerate),
        channels=_non_negative_int(tag.channels),
    )


def _tags_from_mutagen(*, path: Path) -> dict[str, str]:
    """Read normalized tags via mutagen easy mode.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Mapping of tag name to joined string value; empty on failure.
    """
    try:
        audio = mutagen_open(path, easy=True)
    except (MutagenError, OSError, ValueError) as exc:
        logger.debug("mutagen tag read failed for {}: {}", path, exc)
        return {}
    if audio is None or not audio.tags:
        return {}

    result: dict[str, str] = {}
    for key, value in audio.tags.items():
        if isinstance(value, list):
            result[key] = ", ".join(str(item) for item in value)
        else:
            result[key] = str(value)
    return result


def _tags_from_tinytag(*, path: Path) -> dict[str, str]:
    """Read common tags via tinytag as a fallback backend.

    Args:
        path: Filesystem path to the audio file.

    Returns:
        Mapping of common tag names to string values; empty on failure.
    """
    try:
        tag = TinyTag.get(path)
    except (TinyTagException, OSError, ValueError) as exc:
        logger.debug("tinytag tag read failed for {}: {}", path, exc)
        return {}

    candidates = {
        "title": tag.title,
        "artist": tag.artist,
        "album": tag.album,
        "albumartist": tag.albumartist,
        "genre": tag.genre,
        "year": tag.year,
        "track": tag.track,
    }
    return {key: str(value) for key, value in candidates.items() if value}


def _non_negative_int(value: object) -> int | None:
    """Coerce a value into a non-negative integer.

    Args:
        value: Raw backend field value.

    Returns:
        Parsed integer, or ``None`` when missing or invalid.
    """
    if value is None:
        return None
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _non_negative_float(value: object) -> float | None:
    """Coerce a value into a non-negative float.

    Args:
        value: Raw backend field value.

    Returns:
        Parsed float, or ``None`` when missing or invalid.
    """
    if value is None:
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
