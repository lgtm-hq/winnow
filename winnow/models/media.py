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
    :attr:`MediaMetadata.truncated`.
    """

    INCONSISTENT = auto()
    """Packet times contradict each other or the declared duration."""

    CORRUPT = auto()
    """ffprobe failed, warned outside the benign list, or flagged a packet."""

    ESTIMATED = auto()
    """The duration was measured from packets; nothing declared it."""

    TIMEOUT = auto()
    """An ffprobe run breached its time or output bound and was killed."""

    BACKEND_UNAVAILABLE = auto()
    """ffprobe is missing or could not be started."""


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
