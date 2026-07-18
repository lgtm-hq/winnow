"""Tests for the SQLite content-addressable perceptual-hash cache."""

from __future__ import annotations

import sqlite3

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import CacheError
from winnow.hash import _db, CacheEntry, CacheKey, CacheStats, HashCache
from winnow.models.config import CacheSettings
from winnow.models.enums import HashAlgorithm


def _write_media(path: Path, content: bytes = b"pixels") -> None:
    """Write deterministic bytes to a media file used in tests.

    Args:
        path: Destination file path.
        content: Bytes to write.
    """
    path.write_bytes(content)


@pytest.fixture
def cache(tmp_path: Path) -> HashCache:
    """Return a hash cache backed by a temporary on-disk database.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        An open hash cache instance.
    """
    return HashCache(db_path=tmp_path / "cache.db")


def test_default_db_path_under_cache_settings_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-arg cache places its database under the CacheSettings default."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    expected = CacheSettings().directory / "cache.db"

    with HashCache():
        pass

    assert_that(str(expected)).starts_with(str(tmp_path))
    assert_that(expected.is_file()).is_true()


def test_init_creates_parent_directories(tmp_path: Path) -> None:
    """Opening a cache creates missing parent directories for the database."""
    db_path = tmp_path / "nested" / "dir" / "cache.db"

    HashCache(db_path=db_path)

    assert_that(db_path.parent.is_dir()).is_true()


def test_set_and_get_roundtrip(cache: HashCache, tmp_path: Path) -> None:
    """A stored digest is returned for a matching key."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)

    cache.set(key=key, digest="abcd1234")

    assert_that(cache.get(key=key)).is_equal_to("abcd1234")


def test_get_missing_key_returns_none(cache: HashCache, tmp_path: Path) -> None:
    """Looking up an unknown key returns ``None``."""
    key = CacheKey(
        path=tmp_path / "absent.jpg",
        mtime=1.0,
        size=10,
        algorithm=HashAlgorithm.DHASH,
    )

    assert_that(cache.get(key=key)).is_none()


def test_get_invalidated_when_mtime_changes(
    cache: HashCache,
    tmp_path: Path,
) -> None:
    """A key with a changed mtime misses even though the path matches."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    stored = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=stored, digest="digest")

    changed = CacheKey(
        path=stored.path,
        mtime=stored.mtime + 5.0,
        size=stored.size,
        algorithm=stored.algorithm,
    )

    assert_that(cache.get(key=changed)).is_none()


def test_get_invalidated_when_size_changes(
    cache: HashCache,
    tmp_path: Path,
) -> None:
    """A key with a changed size misses even though the path matches."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    stored = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=stored, digest="digest")

    changed = CacheKey(
        path=stored.path,
        mtime=stored.mtime,
        size=stored.size + 100,
        algorithm=stored.algorithm,
    )

    assert_that(cache.get(key=changed)).is_none()


def test_set_replaces_stale_entry_for_same_path(
    cache: HashCache,
    tmp_path: Path,
) -> None:
    """Rewriting a path/algorithm pair overwrites the previous row."""
    media = tmp_path / "photo.jpg"
    _write_media(media, content=b"one")
    first = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=first, digest="first")

    _write_media(media, content=b"two-longer-content")
    second = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=second, digest="second")

    assert_that(cache.get(key=first)).is_none()
    assert_that(cache.get(key=second)).is_equal_to("second")
    assert_that(cache.stats().entry_count).is_equal_to(1)


def test_distinct_algorithms_coexist(cache: HashCache, tmp_path: Path) -> None:
    """The same file can hold digests for multiple algorithms."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    phash_key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    dhash_key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.DHASH)

    cache.set(key=phash_key, digest="phash-digest")
    cache.set(key=dhash_key, digest="dhash-digest")

    assert_that(cache.get(key=phash_key)).is_equal_to("phash-digest")
    assert_that(cache.get(key=dhash_key)).is_equal_to("dhash-digest")
    assert_that(cache.stats().entry_count).is_equal_to(2)


def test_set_many_and_get_many(cache: HashCache, tmp_path: Path) -> None:
    """Batch writes are visible through batch reads."""
    entries = []
    keys = []
    for index in range(3):
        media = tmp_path / f"photo_{index}.jpg"
        _write_media(media, content=f"content-{index}".encode())
        key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
        keys.append(key)
        entries.append(CacheEntry(key=key, digest=f"digest-{index}"))

    cache.set_many(entries=entries)
    results = cache.get_many(keys=keys)

    assert_that(results).is_length(3)
    assert_that(results[keys[0]]).is_equal_to("digest-0")
    assert_that(results[keys[2]]).is_equal_to("digest-2")


def test_get_many_omits_missing_keys(cache: HashCache, tmp_path: Path) -> None:
    """Batch reads return only the keys that hit the cache."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    present = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=present, digest="hit")
    absent = CacheKey(
        path=tmp_path / "missing.jpg",
        mtime=1.0,
        size=1,
        algorithm=HashAlgorithm.PHASH,
    )

    results = cache.get_many(keys=[present, absent])

    assert_that(results).contains_key(present)
    assert_that(results).does_not_contain_key(absent)


def test_set_many_empty_is_noop(cache: HashCache) -> None:
    """Writing an empty batch leaves the cache untouched."""
    cache.set_many(entries=[])

    assert_that(cache.stats().entry_count).is_equal_to(0)


def test_stats_tracks_hits_and_misses(cache: HashCache, tmp_path: Path) -> None:
    """Statistics count hits, misses, entry totals, and derived rates."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=key, digest="digest")
    absent = CacheKey(
        path=tmp_path / "absent.jpg",
        mtime=1.0,
        size=1,
        algorithm=HashAlgorithm.PHASH,
    )

    cache.get(key=key)
    cache.get(key=absent)

    stats = cache.stats()
    assert_that(stats).is_instance_of(CacheStats)
    assert_that(stats.hits).is_equal_to(1)
    assert_that(stats.misses).is_equal_to(1)
    assert_that(stats.entry_count).is_equal_to(1)
    assert_that(stats.lookups).is_equal_to(2)
    assert_that(stats.hit_rate).is_equal_to(0.5)
    assert_that(stats.size_bytes).is_greater_than(0)


def test_stats_hit_rate_zero_without_lookups(cache: HashCache) -> None:
    """The hit rate is zero when no lookups have occurred."""
    stats = cache.stats()

    assert_that(stats.lookups).is_equal_to(0)
    assert_that(stats.hit_rate).is_equal_to(0.0)


def test_prune_stale_removes_entries_for_deleted_files(
    cache: HashCache,
    tmp_path: Path,
) -> None:
    """Pruning drops rows whose source files no longer exist."""
    kept = tmp_path / "kept.jpg"
    removed = tmp_path / "removed.jpg"
    _write_media(kept)
    _write_media(removed)
    kept_key = CacheKey.from_file(path=kept, algorithm=HashAlgorithm.PHASH)
    removed_key = CacheKey.from_file(path=removed, algorithm=HashAlgorithm.PHASH)
    cache.set(key=kept_key, digest="kept")
    cache.set(key=removed_key, digest="removed")

    removed.unlink()
    pruned = cache.prune_stale()

    assert_that(pruned).is_equal_to(1)
    assert_that(cache.get(key=kept_key)).is_equal_to("kept")
    assert_that(cache.stats().entry_count).is_equal_to(1)


def test_prune_stale_returns_zero_when_all_present(
    cache: HashCache,
    tmp_path: Path,
) -> None:
    """Pruning removes nothing when every source file still exists."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=key, digest="digest")

    assert_that(cache.prune_stale()).is_equal_to(0)


def test_clear_removes_all_entries_but_keeps_counters(
    cache: HashCache,
    tmp_path: Path,
) -> None:
    """Clearing empties the cache while preserving session counters."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    cache.set(key=key, digest="digest")
    cache.get(key=key)

    cache.clear()

    stats = cache.stats()
    assert_that(stats.entry_count).is_equal_to(0)
    assert_that(stats.hits).is_equal_to(1)


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Entries written by one instance are visible to a later instance."""
    db_path = tmp_path / "cache.db"
    media = tmp_path / "photo.jpg"
    _write_media(media)
    key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)

    with HashCache(db_path=db_path) as writer:
        writer.set(key=key, digest="persisted")

    reader = HashCache(db_path=db_path)
    assert_that(reader.get(key=key)).is_equal_to("persisted")
    reader.close()


def test_in_memory_database_reports_zero_size() -> None:
    """An in-memory cache works and reports a zero on-disk size."""
    with HashCache(db_path=":memory:") as memory_cache:
        key = CacheKey(
            path=Path("/virtual/photo.jpg"),
            mtime=1.0,
            size=10,
            algorithm=HashAlgorithm.PHASH,
        )
        memory_cache.set(key=key, digest="digest")

        assert_that(memory_cache.get(key=key)).is_equal_to("digest")
        assert_that(memory_cache.stats().size_bytes).is_equal_to(0)


def test_cache_key_from_file_raises_for_missing_file(tmp_path: Path) -> None:
    """Building a key from a missing file raises a cache error."""
    with pytest.raises(CacheError):
        CacheKey.from_file(
            path=tmp_path / "does_not_exist.jpg",
            algorithm=HashAlgorithm.PHASH,
        )


def test_connect_failure_raises_cache_error(tmp_path: Path) -> None:
    """A directory in place of the database file surfaces as a cache error."""
    collision = tmp_path / "cache.db"
    collision.mkdir()

    with pytest.raises(CacheError):
        HashCache(db_path=collision)


def test_query_failure_raises_cache_error(cache: HashCache) -> None:
    """A closed connection turns a lookup into a cache error."""
    key = CacheKey(
        path=Path("/virtual/photo.jpg"),
        mtime=1.0,
        size=1,
        algorithm=HashAlgorithm.PHASH,
    )
    cache.close()

    with pytest.raises(CacheError):
        cache.get(key=key)


def test_set_failure_raises_cache_error(cache: HashCache) -> None:
    """A write against a closed connection surfaces as a cache error."""
    key = CacheKey(
        path=Path("/virtual/photo.jpg"),
        mtime=1.0,
        size=1,
        algorithm=HashAlgorithm.PHASH,
    )
    cache.close()

    with pytest.raises(CacheError):
        cache.set(key=key, digest="digest")


def test_stats_failure_raises_cache_error(cache: HashCache) -> None:
    """A stats query against a closed connection surfaces as a cache error."""
    cache.close()

    with pytest.raises(CacheError):
        cache.stats()


def test_prune_failure_raises_cache_error(cache: HashCache) -> None:
    """A prune scan against a closed connection surfaces as a cache error."""
    cache.close()

    with pytest.raises(CacheError):
        cache.prune_stale()


def test_clear_failure_raises_cache_error(cache: HashCache) -> None:
    """A clear against a closed connection surfaces as a cache error."""
    cache.close()

    with pytest.raises(CacheError):
        cache.clear()


def test_set_many_failure_raises_cache_error(cache: HashCache) -> None:
    """A batch write against a closed connection surfaces as a cache error."""
    key = CacheKey(
        path=Path("/virtual/photo.jpg"),
        mtime=1.0,
        size=1,
        algorithm=HashAlgorithm.PHASH,
    )
    cache.close()

    with pytest.raises(CacheError):
        cache.set_many(entries=[CacheEntry(key=key, digest="digest")])


def test_from_file_stores_absolute_resolved_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key built from a relative path is stored as an absolute path."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    monkeypatch.chdir(tmp_path)

    key = CacheKey.from_file(path="photo.jpg", algorithm=HashAlgorithm.PHASH)

    assert_that(key.path.is_absolute()).is_true()
    assert_that(key.path).is_equal_to(media.resolve())


def test_prune_stale_survives_cwd_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning works after the CWD changes because keys store absolute paths."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    monkeypatch.chdir(tmp_path)
    key = CacheKey.from_file(path="photo.jpg", algorithm=HashAlgorithm.PHASH)

    with HashCache(db_path=tmp_path / "cache.db") as cache:
        cache.set(key=key, digest="digest")
        media.unlink()
        monkeypatch.chdir(tmp_path.parent)

        assert_that(cache.prune_stale()).is_equal_to(1)
        assert_that(cache.stats().entry_count).is_equal_to(0)


def test_on_disk_database_uses_wal_journal(tmp_path: Path) -> None:
    """On-disk databases are opened in write-ahead-logging mode."""
    with HashCache(db_path=tmp_path / "cache.db") as cache:
        mode = cache._connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert_that(mode.lower()).is_equal_to("wal")


def test_in_memory_database_does_not_use_wal_journal() -> None:
    """In-memory databases keep the default journal rather than WAL."""
    with HashCache(db_path=":memory:") as cache:
        mode = cache._connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert_that(mode.lower()).is_equal_to("memory")


def test_get_many_batches_across_chunks(
    cache: HashCache,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batched reads span multiple IN-clause chunks and stay correct."""
    monkeypatch.setattr(_db, "MAX_SQL_VARIABLES", 2)
    keys = []
    for index in range(5):
        media = tmp_path / f"photo_{index}.jpg"
        _write_media(media, content=f"content-{index}".encode())
        key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
        keys.append(key)
        cache.set(key=key, digest=f"digest-{index}")

    results = cache.get_many(keys=keys)

    assert_that(results).is_length(5)
    assert_that(results[keys[4]]).is_equal_to("digest-4")
    assert_that(cache.stats().hits).is_equal_to(5)


def test_prune_stale_batches_across_chunks(
    cache: HashCache,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning deletes across multiple IN-clause chunks in one transaction."""
    monkeypatch.setattr(_db, "MAX_SQL_VARIABLES", 2)
    for index in range(5):
        media = tmp_path / f"photo_{index}.jpg"
        _write_media(media)
        key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
        cache.set(key=key, digest=f"digest-{index}")
        media.unlink()

    assert_that(cache.prune_stale()).is_equal_to(5)
    assert_that(cache.stats().entry_count).is_equal_to(0)


def test_get_many_batches_shared_path_algorithms(
    cache: HashCache,
    tmp_path: Path,
) -> None:
    """A single path with multiple algorithms resolves correctly in a batch."""
    media = tmp_path / "photo.jpg"
    _write_media(media)
    phash_key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.PHASH)
    dhash_key = CacheKey.from_file(path=media, algorithm=HashAlgorithm.DHASH)
    cache.set(key=phash_key, digest="phash-digest")
    cache.set(key=dhash_key, digest="dhash-digest")

    results = cache.get_many(keys=[phash_key, dhash_key])

    assert_that(results[phash_key]).is_equal_to("phash-digest")
    assert_that(results[dhash_key]).is_equal_to("dhash-digest")


def test_get_many_batch_failure_raises_cache_error(cache: HashCache) -> None:
    """A batch read against a closed connection surfaces as a cache error."""
    key = CacheKey(
        path=Path("/virtual/photo.jpg"),
        mtime=1.0,
        size=1,
        algorithm=HashAlgorithm.PHASH,
    )
    cache.close()

    with pytest.raises(CacheError):
        cache.get_many(keys=[key])
