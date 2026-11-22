"""Media file domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum, auto
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

MEDIA_METADATA_SCHEMA_VERSION: Final[int] = 2
"""Schema version of :class:`MediaMetadata`.

Bump on any field change (add, remove, rename, or retype); consumed by the
metadata cache (#42) to invalidate entries persisted under an older shape.
"""


class MediaType(StrEnum):
    """Supported media types discovered during scanning."""

    IMAGE = auto()
    VIDEO = auto()
    AUDIO = auto()


class AudioFlag(StrEnum):
    """Why an audio fact is missing or how far it can be trusted.

    ``truncated`` is not a flag: it has three states and lives in
    :attr:`MediaMetadata.truncated`. ``truncated=True`` and ``CORRUPT``
    claim damage; ``INCONSISTENT``, ``ESTIMATED``, ``UNRECOGNIZED_OUTPUT`` and
    ``UNSUPPORTED`` only say the evidence does not establish a value, and a
    healthy file may carry them.
    """

    INCONSISTENT = auto()
    """The evidence contradicts itself, so the value is not established.

    Packet times contradict each other or the declared duration, an MP4
    packet spans a gap, an FLV packet has no time, or the file changed
    while it was read.
    """

    CORRUPT = auto()
    """The packet walk established damage.

    The walk exited non-zero after an accepted header, or ffprobe flagged a
    packet corrupt. ``corrupt`` is a claim about the file, never about
    ffprobe or about a format winnow does not accept.
    """

    UNRECOGNIZED_OUTPUT = auto()
    """ffprobe wrote a stderr line that is not benign for this file.

    An unexpected line is not proof of damage, so it is not ``corrupt``; it
    still makes every value it could affect unknown. Header output that is
    not valid UTF-8 or JSON is also unrecognized: output ffprobe cannot
    render says nothing about the file.
    """

    UNSUPPORTED = auto()
    """No accepted format recognised the file: the header call exited non-zero.

    This covers formats outside the allow-list, garbage, and playlists. It is
    not a damage claim.
    """

    ESTIMATED = auto()
    """The duration was measured from packets; nothing declared it."""

    TIMEOUT = auto()
    """An ffprobe run breached its time or output bound and was killed."""

    BACKEND_UNAVAILABLE = auto()
    """ffprobe is missing, could not be started, or failed as a program.

    A signal, exit 126 or 127, or an unreadable ``ffprobe -version`` says
    nothing about the file.
    """


class MediaMetadata(BaseModel):
    """Extended metadata extracted from a media file.

    The audio reader also fills :attr:`container`, :attr:`audio_stream_count`,
    :attr:`probe_score`, :attr:`truncated`, :attr:`audio_flags`, and
    :attr:`audio_backend_reason`. ``truncated`` is ``True`` or ``False`` only
    when a packet walk checked it; ``None`` means not checked.
    """

    model_config = ConfigDict(validate_assignment=True)

    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    codec: str | None = None
    bitrate: int | None = Field(default=None, ge=0)
    frame_rate: float | None = Field(default=None, ge=0)
    sample_rate: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    image_format: str | None = None
    color_mode: str | None = None
    bit_depth: int | None = Field(default=None, ge=0)
    has_alpha: bool | None = None
    captured_at: datetime | None = None
    container: str | None = None
    audio_stream_count: int | None = Field(default=None, ge=0)
    probe_score: int | None = Field(default=None, ge=0, le=100)
    truncated: bool | None = None
    audio_flags: tuple[AudioFlag, ...] | None = None
    audio_backend_reason: str | None = None

    @field_validator("audio_flags")
    @classmethod
    def _normalize_audio_flags(
        cls,
        value: tuple[AudioFlag, ...] | None,
    ) -> tuple[AudioFlag, ...] | None:
        """Sort audio flags and drop repeats.

        Args:
            value: Flags as given.

        Returns:
            The flags in declaration order without repeats, or ``None``.
        """
        if value is None:
            return None
        order = list(AudioFlag)
        return tuple(sorted(set(value), key=order.index))


class MediaFile(BaseModel):
    """Represents a discovered media file before organization."""

    model_config = ConfigDict(validate_assignment=True)

    path: Path
    media_type: MediaType
    creation_date: datetime
    extension: str
    size_bytes: int = Field(ge=0)
    metadata: MediaMetadata | None = None
    live_photo_id: str | None = None
