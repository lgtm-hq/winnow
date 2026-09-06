"""A media file paired with its computed perceptual hash."""

from __future__ import annotations

from dataclasses import dataclass

from winnow.hash.image_hasher import PerceptualHash
from winnow.models.media import MediaFile


@dataclass(frozen=True, slots=True)
class HashedMedia:
    """A media file together with the perceptual hash computed for it.

    Args:
        media: The media file that was hashed.
        perceptual_hash: Perceptual hash of ``media``.
        from_cache: ``True`` when the hash was served from the hash cache
            rather than computed in this batch.
    """

    media: MediaFile
    perceptual_hash: PerceptualHash
    from_cache: bool
