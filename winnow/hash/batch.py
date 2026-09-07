"""Cache-first batch hashing over any :class:`PerceptualHasher`.

:func:`hash_media_files` is the single building block shared by the ``scan``
workflow, the Deduplication pipeline step, and the API: it resolves cache
hits in one round-trip, hashes the misses concurrently, writes the new
digests back in one transaction, and returns results in input order.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from winnow.exceptions import CacheError, HashError
from winnow.hash.cache_entry import CacheEntry
from winnow.hash.cache_key import CacheKey
from winnow.hash.hash_failure import HashFailure
from winnow.hash.hashed_media import HashedMedia
from winnow.hash.image_hasher import PerceptualHash

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from winnow.hash.cache import HashCache
    from winnow.hash.protocol import PerceptualHasher
    from winnow.models.media import MediaFile, MediaType

_OPERATION = "hash_media_files"


@dataclass(frozen=True, slots=True)
class BatchHashResult:
    """Outcome of one :func:`hash_media_files` call.

    Args:
        hashed: Successfully hashed files, in the order they were supplied.
        skipped: Paths whose media type had no hasher registered.
        failures: Files that raised a per-file hashing or cache-key error.
    """

    hashed: list[HashedMedia]
    skipped: list[Path]
    failures: list[HashFailure]


@dataclass(frozen=True, slots=True)
class _Job:
    """A file that has a hasher and a cache key, awaiting a digest.

    Args:
        index: Position of the file in the input sequence.
        media: The media file to hash.
        hasher: Hasher selected for ``media.media_type``.
        key: Cache key identifying the file for ``hasher``.
    """

    index: int
    media: MediaFile
    hasher: PerceptualHasher
    key: CacheKey


def _hash_job(job: _Job) -> HashedMedia | HashFailure:
    """Hash one cache miss, converting a per-file error into a failure.

    Args:
        job: The file to hash.

    Returns:
        The hashed media, or a failure when the hasher raised ``HashError``.
    """
    try:
        perceptual_hash = job.hasher.hash_file(job.media.path)
    except HashError as exc:
        return HashFailure(path=job.media.path, error=exc)
    return HashedMedia(
        media=job.media,
        perceptual_hash=perceptual_hash,
        from_cache=False,
    )


def _from_cache(job: _Job, digest: str) -> HashedMedia | HashFailure:
    """Rebuild a hashed result from a cached serialized digest.

    Args:
        job: The file the digest belongs to.
        digest: Serialized :class:`PerceptualHash` read from the cache.

    Returns:
        The hashed media, or a failure when the stored digest is malformed.
    """
    try:
        perceptual_hash = PerceptualHash.deserialize(digest)
    except HashError as exc:
        return HashFailure(path=job.media.path, error=exc)
    return HashedMedia(
        media=job.media, perceptual_hash=perceptual_hash, from_cache=True
    )


def _prepare_jobs(
    files: Iterable[MediaFile],
    hashers: Mapping[MediaType, PerceptualHasher],
) -> tuple[list[_Job], list[Path], dict[int, HashFailure]]:
    """Pair each file with a hasher and cache key.

    Args:
        files: Media files to hash.
        hashers: Hasher to use per media type.

    Returns:
        The jobs to run, the skipped paths, and per-index failures for files
        whose cache key could not be built.
    """
    jobs: list[_Job] = []
    skipped: list[Path] = []
    failures: dict[int, HashFailure] = {}
    for index, media in enumerate(files):
        hasher = hashers.get(media.media_type)
        if hasher is None:
            skipped.append(media.path)
            continue
        try:
            key = CacheKey.from_file(media.path, hasher.cache_algorithm)
        except CacheError as exc:
            failures[index] = HashFailure(path=media.path, error=exc)
            continue
        jobs.append(_Job(index=index, media=media, hasher=hasher, key=key))
    return jobs, skipped, failures


def _hash_misses(
    misses: list[_Job],
    *,
    workers: int,
) -> list[HashedMedia | HashFailure]:
    """Hash cache misses concurrently, preserving ``misses`` order.

    Args:
        misses: Jobs whose digest was not in the cache.
        workers: Thread pool size.

    Returns:
        One outcome per job, aligned with ``misses``.
    """
    if not misses:
        return []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(_hash_job, misses))


def hash_media_files(
    files: Iterable[MediaFile],
    *,
    hashers: Mapping[MediaType, PerceptualHasher],
    cache: HashCache | None = None,
    workers: int = 1,
) -> BatchHashResult:
    """Hash media files cache-first, in parallel, preserving input order.

    Files whose ``media_type`` has no entry in ``hashers`` are reported as
    skipped. For the rest, cache keys are built from the file's metadata and
    the hasher's :attr:`~PerceptualHasher.cache_algorithm`; hits are resolved
    with one ``get_many`` call, misses are hashed in a thread pool, and the
    new digests are persisted with one ``set_many`` call. A ``HashError`` or
    ``CacheError`` raised for a single file becomes a :class:`HashFailure`
    instead of aborting the batch.

    Args:
        files: Media files to hash.
        hashers: Hasher to use for each media type.
        cache: Hash cache to consult and populate; ``None`` disables caching.
        workers: Number of threads used to hash cache misses.

    Returns:
        Hashed, skipped, and failed files. ``hashed`` follows input order.

    Raises:
        HashError: If ``workers`` is less than one.
        CacheError: If a whole-batch cache read or write fails.
    """
    if workers < 1:
        raise HashError(
            "workers must be at least 1",
            operation=_OPERATION,
            details={"workers": workers},
        )
    jobs, skipped, failures = _prepare_jobs(files, hashers)
    outcomes: dict[int, HashedMedia | HashFailure] = dict(failures)

    cached = cache.get_many(job.key for job in jobs) if cache is not None else {}
    misses: list[_Job] = []
    for job in jobs:
        digest = cached.get(job.key)
        if digest is None:
            misses.append(job)
        else:
            outcomes[job.index] = _from_cache(job, digest)

    fresh = _hash_misses(misses, workers=workers)
    for job, outcome in zip(misses, fresh, strict=True):
        outcomes[job.index] = outcome
    if cache is not None:
        cache.set_many(
            CacheEntry(key=job.key, digest=outcome.perceptual_hash.serialize())
            for job, outcome in zip(misses, fresh, strict=True)
            if isinstance(outcome, HashedMedia)
        )

    ordered = [outcomes[index] for index in sorted(outcomes)]
    return BatchHashResult(
        hashed=[item for item in ordered if isinstance(item, HashedMedia)],
        skipped=skipped,
        failures=[item for item in ordered if isinstance(item, HashFailure)],
    )
