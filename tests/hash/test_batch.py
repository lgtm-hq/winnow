"""Tests for the cache-first batch hasher ``hash_media_files``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from winnow.exceptions import CacheError, HashError
from winnow.hash import (
    BatchHashResult,
    CacheKey,
    HashCache,
    HashFailure,
    ImageHasher,
    PerceptualHash,
    hash_media_files,
)
from winnow.models.enums import HashAlgorithm
from winnow.models.media import MediaFile, MediaType

_BATCH_SIZE = 20


class _CountingHasher:
    """Fake hasher that records how many times ``hash_file`` is invoked."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def cache_algorithm(self) -> str:
        """Return a stable fake identity.

        Returns:
            Identity string distinct from the real image hasher.
        """
        return "fake:8"

    def hash_file(self, path: Path) -> PerceptualHash:
        """Return a digest derived from the file name and count the call.

        Args:
            path: File being hashed.

        Returns:
            A deterministic perceptual hash unique to ``path.name``.
        """
        self.calls += 1
        digest = f"{abs(hash(path.name)) & 0xFFFFFFFFFFFFFFFF:016x}"
        return PerceptualHash(algorithm=HashAlgorithm.PHASH, hash_size=8, digest=digest)


def _write_image(path: Path, colour: str) -> Path:
    """Save a solid-colour image to ``path`` and return it.

    Args:
        path: Destination path; suffix selects the format.
        colour: Pillow colour name.

    Returns:
        The written path.
    """
    Image.new("RGB", (64, 64), colour).save(path)
    return path


def _media(path: Path, media_type: MediaType = MediaType.IMAGE) -> MediaFile:
    """Wrap ``path`` in a ``MediaFile`` model.

    Args:
        path: File on disk.
        media_type: Media type to record.

    Returns:
        A media file model for ``path``.
    """
    return MediaFile(
        path=path,
        media_type=media_type,
        creation_date=datetime(2024, 1, 1, tzinfo=UTC),
        extension=path.suffix,
        size_bytes=path.stat().st_size if path.exists() else 0,
    )


@pytest.fixture
def image_files(tmp_path: Path) -> list[MediaFile]:
    """Two identical JPEGs and one PNG in ``tmp_path``.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Media files in the order ``a.jpg``, ``a_copy.jpg``, ``b.png``.
    """
    first = _write_image(tmp_path / "a.jpg", "red")
    copy = tmp_path / "a_copy.jpg"
    copy.write_bytes(first.read_bytes())
    other = _write_image(tmp_path / "b.png", "blue")
    return [_media(first), _media(copy), _media(other)]


def test_hashes_in_input_order_without_cache(image_files: list[MediaFile]) -> None:
    """Every file is hashed fresh and ``hashed`` follows the input order."""
    result = hash_media_files(
        image_files,
        hashers={MediaType.IMAGE: ImageHasher()},
    )

    assert_that(result).is_instance_of(BatchHashResult)
    assert_that([item.media.path for item in result.hashed]).is_equal_to(
        [media.path for media in image_files],
    )
    assert_that([item.from_cache for item in result.hashed]).is_equal_to(
        [False, False, False],
    )
    assert_that(result.hashed[0].perceptual_hash).is_equal_to(
        result.hashed[1].perceptual_hash,
    )
    assert_that(result.skipped).is_empty()
    assert_that(result.failures).is_empty()


def test_second_run_is_served_from_cache(
    image_files: list[MediaFile],
    tmp_path: Path,
) -> None:
    """A repeat call with the same cache hits for every file and never hashes."""
    hasher = _CountingHasher()
    with HashCache(db_path=tmp_path / "cache.db") as cache:
        first = hash_media_files(
            image_files,
            hashers={MediaType.IMAGE: hasher},
            cache=cache,
        )
        assert_that(hasher.calls).is_equal_to(len(image_files))
        hasher.calls = 0

        second = hash_media_files(
            image_files,
            hashers={MediaType.IMAGE: hasher},
            cache=cache,
        )

    assert_that(hasher.calls).is_equal_to(0)
    assert_that([item.from_cache for item in second.hashed]).is_equal_to(
        [True, True, True],
    )
    assert_that([item.perceptual_hash for item in second.hashed]).is_equal_to(
        [item.perceptual_hash for item in first.hashed],
    )


def test_corrupt_file_becomes_failure_while_others_hash(
    image_files: list[MediaFile],
    tmp_path: Path,
) -> None:
    """A corrupt JPEG lands in ``failures`` with a ``HashError``."""
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not a jpeg")
    files = [image_files[0], _media(corrupt), image_files[2]]

    result = hash_media_files(files, hashers={MediaType.IMAGE: ImageHasher()})

    assert_that([item.media.path for item in result.hashed]).is_equal_to(
        [image_files[0].path, image_files[2].path],
    )
    assert_that(result.failures).is_length(1)
    failure = result.failures[0]
    assert_that(failure).is_instance_of(HashFailure)
    assert_that(failure.path).is_equal_to(corrupt)
    assert_that(failure.error).is_instance_of(HashError)


def test_missing_file_becomes_cache_error_failure(tmp_path: Path) -> None:
    """A file that vanished before keying is reported with a ``CacheError``."""
    missing = _media(tmp_path / "gone.jpg")

    result = hash_media_files(
        [missing],
        hashers={MediaType.IMAGE: ImageHasher()},
    )

    assert_that(result.hashed).is_empty()
    assert_that(result.failures).is_length(1)
    assert_that(result.failures[0].error).is_instance_of(CacheError)


def test_unsupported_media_type_is_skipped(
    image_files: list[MediaFile],
    tmp_path: Path,
) -> None:
    """A file whose media type has no hasher lands in ``skipped``."""
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"\x00")
    files = [*image_files, _media(audio, media_type=MediaType.AUDIO)]

    result = hash_media_files(files, hashers={MediaType.IMAGE: ImageHasher()})

    assert_that(result.skipped).is_equal_to([audio])
    assert_that(result.hashed).is_length(len(image_files))
    assert_that(result.failures).is_empty()


@pytest.mark.parametrize("workers", [1, 4], ids=["workers=1", "workers=4"])
def test_parallel_output_preserves_input_order(tmp_path: Path, workers: int) -> None:
    """Twenty files hashed with a thread pool come back in input order."""
    files = [
        _media(_write_image(tmp_path / f"{index:02d}.png", "green"))
        for index in range(_BATCH_SIZE)
    ]

    result = hash_media_files(
        files,
        hashers={MediaType.IMAGE: _CountingHasher()},
        workers=workers,
    )

    assert_that([item.media.path for item in result.hashed]).is_equal_to(
        [media.path for media in files],
    )
    assert_that(result.failures).is_empty()


def test_zero_workers_raises_hash_error(image_files: list[MediaFile]) -> None:
    """``workers=0`` is rejected before any file is touched."""
    with pytest.raises(HashError) as excinfo:
        hash_media_files(
            image_files,
            hashers={MediaType.IMAGE: ImageHasher()},
            workers=0,
        )
    assert_that(excinfo.value.context.operation).is_equal_to("hash_media_files")


def test_malformed_cached_digest_becomes_failure(
    image_files: list[MediaFile],
    tmp_path: Path,
) -> None:
    """A cache hit that cannot be deserialized is reported, not raised."""
    hasher = _CountingHasher()
    with HashCache(db_path=tmp_path / "cache.db") as cache:
        hash_media_files(
            image_files[:1],
            hashers={MediaType.IMAGE: hasher},
            cache=cache,
        )
        key = CacheKey.from_file(image_files[0].path, hasher.cache_algorithm)
        cache.set(key, "garbage")

        result = hash_media_files(
            image_files[:1],
            hashers={MediaType.IMAGE: hasher},
            cache=cache,
        )

    assert_that(result.hashed).is_empty()
    assert_that(result.failures).is_length(1)
    assert_that(result.failures[0].error).is_instance_of(HashError)
