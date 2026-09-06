"""Exact content hashing (MD5 / SHA-256) for byte-identical duplicate detection.

Content hashes are not :class:`~winnow.hash.image_hasher.PerceptualHash` values:
Hamming distance between two SHA-256 digests is meaningless. They get their own
value type and a sibling :class:`ContentHasherProtocol` sharing the
``cache_algorithm`` + ``hash_file`` shape of
:class:`~winnow.hash.protocol.PerceptualHasher`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from string import hexdigits
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, Self, runtime_checkable

from winnow.exceptions import HashError
from winnow.models.enums import HashAlgorithm

if TYPE_CHECKING:
    from collections.abc import Mapping

    from winnow.models.config import WinnowConfig

CONTENT_ALGORITHMS: frozenset[HashAlgorithm] = frozenset(
    {HashAlgorithm.MD5, HashAlgorithm.SHA256},
)
"""Subset of :class:`HashAlgorithm` values that are exact content digests."""

DEFAULT_CHUNK_SIZE: Final[int] = 1024 * 1024
"""Default number of bytes read per block when hashing a file."""

_DIGEST_LENGTHS: Mapping[HashAlgorithm, int] = MappingProxyType(
    {HashAlgorithm.MD5: 32, HashAlgorithm.SHA256: 64},
)
_LOWER_HEXDIGITS = frozenset(hexdigits.lower())
_DESERIALIZE_OPERATION = "deserialize_content_hash"
_CONFIGURE_OPERATION = "configure_content_hasher"


def _validate_algorithm(algorithm: HashAlgorithm, *, operation: str) -> None:
    """Ensure ``algorithm`` is an exact content hash algorithm.

    Args:
        algorithm: Algorithm to validate.
        operation: Operation name recorded on any raised error.

    Raises:
        HashError: If ``algorithm`` is not in :data:`CONTENT_ALGORITHMS`.
    """
    if algorithm not in CONTENT_ALGORITHMS:
        raise HashError(
            "algorithm is not a content hash algorithm",
            operation=operation,
            details={
                "algorithm": str(algorithm),
                "supported": sorted(member.value for member in CONTENT_ALGORITHMS),
            },
        )


@dataclass(frozen=True, slots=True)
class ContentHash:
    """An exact content digest and the algorithm that produced it.

    Args:
        algorithm: Content hash algorithm (MD5 or SHA-256).
        digest: Lowercase hexadecimal digest, 32 (MD5) or 64 (SHA-256) chars.
    """

    algorithm: HashAlgorithm
    digest: str

    def serialize(self) -> str:
        """Return a stable string encoding of this hash.

        The format is ``"{algorithm}:{digest}"``, for example ``"sha256:9f86..."``.

        Returns:
            Serialized hash string.
        """
        return f"{self.algorithm.value}:{self.digest}"

    @classmethod
    def deserialize(cls, value: str) -> Self:
        """Reconstruct a hash from its :meth:`serialize` representation.

        Args:
            value: Serialized string of the form ``"{algorithm}:{digest}"``.

        Returns:
            Reconstructed content hash.

        Raises:
            HashError: If ``value`` has no ``:`` separator, names a
                non-content algorithm, or carries a digest that is not
                lowercase hex of the algorithm's expected length.
        """
        algorithm_value, separator, digest = value.partition(":")
        if not separator:
            raise HashError(
                "malformed serialized content hash",
                operation=_DESERIALIZE_OPERATION,
                details={"value": value},
            )
        try:
            algorithm = HashAlgorithm(algorithm_value)
        except ValueError as error:
            raise HashError(
                "unknown content hash algorithm",
                operation=_DESERIALIZE_OPERATION,
                details={"algorithm": algorithm_value},
            ) from error
        _validate_algorithm(algorithm, operation=_DESERIALIZE_OPERATION)
        expected_length = _DIGEST_LENGTHS[algorithm]
        if len(digest) != expected_length or not set(digest) <= _LOWER_HEXDIGITS:
            raise HashError(
                "content hash digest must be lowercase hex of the expected length",
                operation=_DESERIALIZE_OPERATION,
                details={
                    "algorithm": algorithm.value,
                    "digest": digest,
                    "expected_length": expected_length,
                },
            )
        return cls(algorithm=algorithm, digest=digest)


@runtime_checkable
class ContentHasherProtocol(Protocol):
    """Hash a file's bytes into a :class:`ContentHash`.

    Sibling of :class:`~winnow.hash.protocol.PerceptualHasher` with the same
    member shape but a return type on which similarity is undefined.
    """

    @property
    def cache_algorithm(self) -> str:
        """Identity string for the hash cache, for example ``"content-sha256"``.

        Returns:
            Stable, human-readable hasher identity.
        """
        ...

    def hash_file(self, path: Path) -> ContentHash:
        """Compute the exact content hash of the file at ``path``.

        Args:
            path: Filesystem path of the file to hash.

        Returns:
            Computed content hash.

        Raises:
            HashError: If the file cannot be read.
        """
        ...


@dataclass(frozen=True, slots=True)
class ContentHasher:
    """Compute exact content digests with a fixed algorithm and block size.

    Args:
        algorithm: Content hash algorithm. Defaults to SHA-256.
        chunk_size: Bytes read per block when hashing a file. Defaults to
            :data:`DEFAULT_CHUNK_SIZE`.

    Raises:
        HashError: If ``algorithm`` is not a content hash algorithm or
            ``chunk_size`` is below one.
    """

    algorithm: HashAlgorithm = HashAlgorithm.SHA256
    chunk_size: int = DEFAULT_CHUNK_SIZE

    def __post_init__(self) -> None:
        """Validate the configured algorithm and chunk size."""
        _validate_algorithm(self.algorithm, operation=_CONFIGURE_OPERATION)
        if self.chunk_size < 1:
            raise HashError(
                "chunk_size must be at least 1",
                operation=_CONFIGURE_OPERATION,
                details={"chunk_size": self.chunk_size},
            )

    @property
    def cache_algorithm(self) -> str:
        """Identity string for the hash cache, for example ``"content-sha256"``.

        Returns:
            Stable hasher identity satisfying :class:`ContentHasherProtocol`.
        """
        return f"content-{self.algorithm.value}"

    @classmethod
    def from_config(cls, config: WinnowConfig | None = None) -> Self:
        """Build a hasher from ``config.hash_algorithm``.

        Args:
            config: Configuration to read the algorithm from. ``None`` yields
                the default hasher.

        Returns:
            Hasher using the configured algorithm and the default chunk size.

        Raises:
            HashError: If the configured algorithm is not a content hash
                algorithm.
        """
        if config is None:
            return cls()
        return cls(algorithm=config.hash_algorithm)

    def _new_digest(self) -> hashlib._Hash:
        """Return a fresh hash object; ``usedforsecurity=False`` keeps MD5 on FIPS.

        Returns:
            Hash object ready to accept bytes.
        """
        return hashlib.new(self.algorithm.value, usedforsecurity=False)

    def hash_bytes(self, data: bytes) -> ContentHash:
        """Compute the content hash of an in-memory byte string.

        Args:
            data: Bytes to hash.

        Returns:
            Computed content hash.
        """
        digest = self._new_digest()
        digest.update(data)
        return ContentHash(algorithm=self.algorithm, digest=digest.hexdigest())

    def hash_file(self, path: Path) -> ContentHash:
        """Compute the content hash of the file at ``path``.

        The file is streamed in :attr:`chunk_size` blocks.

        Args:
            path: Filesystem path of the file to hash.

        Returns:
            Computed content hash.

        Raises:
            HashError: If ``path`` is missing, is a directory, or cannot be
                read; the underlying :class:`OSError` is chained.
        """
        digest = self._new_digest()
        try:
            with Path(path).open("rb") as handle:
                while chunk := handle.read(self.chunk_size):
                    digest.update(chunk)
        except OSError as error:
            raise HashError(
                "failed to read file for content hashing",
                operation="hash_content",
                file_path=path,
            ) from error
        return ContentHash(algorithm=self.algorithm, digest=digest.hexdigest())


__all__ = [
    "CONTENT_ALGORITHMS",
    "DEFAULT_CHUNK_SIZE",
    "ContentHash",
    "ContentHasher",
    "ContentHasherProtocol",
]
