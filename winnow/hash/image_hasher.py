"""Perceptual image hashing built on the :mod:`imagehash` library.

This module wraps average (aHash), difference (dHash), perceptual (pHash), and
wavelet (wHash) hashing so callers get a stable serialization format, a
Hamming-distance helper, and domain :class:`~winnow.exceptions.HashError`
failures instead of raw library or Pillow exceptions.

Hashes are square: an ``ImageHasher`` with ``hash_size`` ``n`` produces
``n * n`` bits. Two hashes are only comparable when they share the same
algorithm and hash size.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Self

import imagehash
from PIL import Image, UnidentifiedImageError

from winnow.exceptions import HashError
from winnow.models.enums import HashAlgorithm

if TYPE_CHECKING:
    from os import PathLike

DEFAULT_HASH_SIZE = 8
"""Default edge length for a computed hash (an 8x8, 64-bit hash)."""

PERCEPTUAL_ALGORITHMS: frozenset[HashAlgorithm] = frozenset(
    {
        HashAlgorithm.AHASH,
        HashAlgorithm.DHASH,
        HashAlgorithm.PHASH,
        HashAlgorithm.WHASH,
    },
)
"""Subset of :class:`HashAlgorithm` values supported by perceptual hashing."""

_SERIALIZED_FIELD_COUNT = 3
_HASH_FUNCTIONS: Mapping[
    HashAlgorithm,
    Callable[..., imagehash.ImageHash],
] = MappingProxyType(
    {
        HashAlgorithm.AHASH: imagehash.average_hash,
        HashAlgorithm.DHASH: imagehash.dhash,
        HashAlgorithm.PHASH: imagehash.phash,
        HashAlgorithm.WHASH: imagehash.whash,
    },
)


def _is_power_of_two(value: int) -> bool:
    """Return whether ``value`` is a positive power of two.

    Args:
        value: Integer to test.

    Returns:
        ``True`` when ``value`` is a positive power of two.
    """
    return value > 0 and (value & (value - 1)) == 0


def _validate_hash_parameters(
    *,
    algorithm: HashAlgorithm,
    hash_size: int,
    operation: str,
) -> None:
    """Validate a perceptual hash algorithm and size combination.

    Args:
        algorithm: Algorithm to validate.
        hash_size: Edge length to validate.
        operation: Operation name recorded on any raised error.

    Raises:
        HashError: If ``algorithm`` is not a perceptual hash algorithm,
            ``hash_size`` is below two, or wHash is requested with a
            non-power-of-two ``hash_size``.
    """
    if algorithm not in PERCEPTUAL_ALGORITHMS:
        raise HashError(
            "algorithm is not a perceptual hash algorithm",
            operation=operation,
            details={
                "algorithm": str(algorithm),
                "supported": sorted(member.value for member in PERCEPTUAL_ALGORITHMS),
            },
        )
    if hash_size < 2:
        raise HashError(
            "hash_size must be at least 2",
            operation=operation,
            details={"hash_size": hash_size},
        )
    if algorithm is HashAlgorithm.WHASH and not _is_power_of_two(hash_size):
        raise HashError(
            "whash requires a power-of-two hash_size",
            operation=operation,
            details={"hash_size": hash_size},
        )


def _decode_hash(digest: str) -> imagehash.ImageHash:
    """Decode a hexadecimal digest into an :class:`imagehash.ImageHash`.

    Args:
        digest: Hexadecimal string produced by a perceptual hash.

    Returns:
        Reconstructed hash usable for Hamming-distance comparison.

    Raises:
        HashError: If ``digest`` is not valid hexadecimal hash data.
    """
    try:
        return imagehash.hex_to_hash(digest)
    except (ValueError, TypeError) as error:
        raise HashError(
            "invalid perceptual hash digest",
            operation="decode_hash",
            details={"digest": digest},
        ) from error


def hamming_distance(left: str, right: str) -> int:
    """Return the Hamming distance between two serialized hash digests.

    Args:
        left: Hexadecimal digest of the first hash.
        right: Hexadecimal digest of the second hash.

    Returns:
        Number of differing bits between the two hashes.

    Raises:
        HashError: If either digest is invalid or the two hashes have
            incompatible shapes.
    """
    if len(left) != len(right):
        raise HashError(
            "cannot compare perceptual hashes of different sizes",
            operation="hamming_distance",
            details={
                "left_bits": len(left) * 4,
                "right_bits": len(right) * 4,
            },
        )
    left_hash = _decode_hash(left)
    right_hash = _decode_hash(right)
    return int(left_hash - right_hash)


@dataclass(frozen=True, slots=True)
class PerceptualHash:
    """A computed perceptual hash and the parameters used to produce it.

    Args:
        algorithm: Algorithm that produced ``digest``.
        hash_size: Edge length used when hashing; the hash holds
            ``hash_size ** 2`` bits.
        digest: Hexadecimal string encoding the hash bits.
    """

    algorithm: HashAlgorithm
    hash_size: int
    digest: str

    def distance(self, other: PerceptualHash) -> int:
        """Return the Hamming distance to another compatible hash.

        Args:
            other: Hash to compare against.

        Returns:
            Number of differing bits between the two hashes.

        Raises:
            HashError: If the two hashes use different algorithms or sizes.
        """
        if self.algorithm is not other.algorithm or self.hash_size != other.hash_size:
            raise HashError(
                "cannot compare hashes from different algorithms or sizes",
                operation="distance",
                details={
                    "left": self.serialize(),
                    "right": other.serialize(),
                },
            )
        return hamming_distance(self.digest, other.digest)

    def is_similar(self, other: PerceptualHash, *, threshold: int) -> bool:
        """Return whether another hash is within ``threshold`` bits.

        Args:
            other: Hash to compare against.
            threshold: Inclusive maximum Hamming distance to treat as similar.

        Returns:
            ``True`` when the Hamming distance is less than or equal to
            ``threshold``.

        Raises:
            HashError: If the two hashes are incompatible or ``threshold`` is
                negative.
        """
        if threshold < 0:
            raise HashError(
                "similarity threshold must not be negative",
                operation="is_similar",
                details={"threshold": threshold},
            )
        return self.distance(other) <= threshold

    def serialize(self) -> str:
        """Return a stable string encoding of this hash.

        The format is ``"{algorithm}:{hash_size}:{digest}"``, for example
        ``"phash:8:8000000000000000"``.

        Returns:
            Serialized hash string.
        """
        return f"{self.algorithm.value}:{self.hash_size}:{self.digest}"

    @classmethod
    def deserialize(cls, value: str) -> Self:
        """Reconstruct a hash from its :meth:`serialize` representation.

        Args:
            value: Serialized string of the form
                ``"{algorithm}:{hash_size}:{digest}"``.

        Returns:
            Reconstructed perceptual hash.

        Raises:
            HashError: If ``value`` is malformed or names an unsupported
                algorithm.
        """
        parts = value.split(":", _SERIALIZED_FIELD_COUNT - 1)
        if len(parts) != _SERIALIZED_FIELD_COUNT:
            raise HashError(
                "malformed serialized perceptual hash",
                operation="deserialize",
                details={"value": value},
            )
        algorithm_value, hash_size_value, digest = parts
        try:
            algorithm = HashAlgorithm(algorithm_value)
        except ValueError as error:
            raise HashError(
                "unknown perceptual hash algorithm",
                operation="deserialize",
                details={"algorithm": algorithm_value},
            ) from error
        try:
            hash_size = int(hash_size_value)
        except ValueError as error:
            raise HashError(
                "malformed serialized perceptual hash",
                operation="deserialize",
                details={"value": value},
            ) from error
        _validate_hash_parameters(
            algorithm=algorithm,
            hash_size=hash_size,
            operation="deserialize",
        )
        return cls(algorithm=algorithm, hash_size=hash_size, digest=digest)


@dataclass(frozen=True, slots=True)
class ImageHasher:
    """Compute perceptual hashes with a fixed algorithm and size.

    Args:
        algorithm: Perceptual hash algorithm to apply. Defaults to pHash.
        hash_size: Edge length of the square hash; the hash contains
            ``hash_size ** 2`` bits. Defaults to :data:`DEFAULT_HASH_SIZE`.

    Raises:
        HashError: If ``algorithm`` is not a perceptual hash algorithm,
            ``hash_size`` is below two, or wHash is requested with a
            non-power-of-two ``hash_size``.
    """

    algorithm: HashAlgorithm = HashAlgorithm.PHASH
    hash_size: int = DEFAULT_HASH_SIZE

    def __post_init__(self) -> None:
        """Validate the configured algorithm and hash size."""
        _validate_hash_parameters(
            algorithm=self.algorithm,
            hash_size=self.hash_size,
            operation="configure_hasher",
        )

    def hash(self, image: Image.Image) -> PerceptualHash:
        """Compute the perceptual hash of an in-memory image.

        Args:
            image: Pillow image to hash. Any mode is accepted; the underlying
                algorithm converts to grayscale as needed.

        Returns:
            Computed perceptual hash.

        Raises:
            HashError: If ``image`` is not a Pillow image or hashing fails.
        """
        if not isinstance(image, Image.Image):
            raise HashError(
                "image must be a PIL.Image.Image instance",
                operation="hash_image",
                details={"received": type(image).__name__},
            )
        hash_function = _HASH_FUNCTIONS[self.algorithm]
        try:
            computed = hash_function(image, hash_size=self.hash_size)
        except (ValueError, TypeError, OSError) as error:
            raise HashError(
                "failed to compute perceptual hash",
                operation="hash_image",
                details={
                    "algorithm": self.algorithm.value,
                    "hash_size": self.hash_size,
                },
            ) from error
        return PerceptualHash(
            algorithm=self.algorithm,
            hash_size=self.hash_size,
            digest=str(computed),
        )

    def hash_file(self, path: str | PathLike[str]) -> PerceptualHash:
        """Open an image file and compute its perceptual hash.

        Args:
            path: Filesystem path to an image readable by Pillow.

        Returns:
            Computed perceptual hash.

        Raises:
            HashError: If the file cannot be opened, decoded, or hashed.
        """
        file_path = Path(path)
        try:
            with Image.open(file_path) as image:
                image.load()
                return self.hash(image)
        except (OSError, UnidentifiedImageError, ValueError) as error:
            raise HashError(
                "failed to read image file for hashing",
                operation="hash_file",
                file_path=file_path,
                details={
                    "algorithm": self.algorithm.value,
                    "hash_size": self.hash_size,
                },
            ) from error


def hash_image(
    image: Image.Image,
    *,
    algorithm: HashAlgorithm = HashAlgorithm.PHASH,
    hash_size: int = DEFAULT_HASH_SIZE,
) -> PerceptualHash:
    """Compute the perceptual hash of an image with a one-off hasher.

    Args:
        image: Pillow image to hash.
        algorithm: Perceptual hash algorithm to apply. Defaults to pHash.
        hash_size: Edge length of the square hash. Defaults to
            :data:`DEFAULT_HASH_SIZE`.

    Returns:
        Computed perceptual hash.

    Raises:
        HashError: If the configuration is invalid or hashing fails.
    """
    return ImageHasher(algorithm=algorithm, hash_size=hash_size).hash(image)


__all__ = [
    "DEFAULT_HASH_SIZE",
    "PERCEPTUAL_ALGORITHMS",
    "ImageHasher",
    "PerceptualHash",
    "hamming_distance",
    "hash_image",
]
