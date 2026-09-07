"""Tests for the SQLite media-metadata cache."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import CacheError
from winnow.hash import (
    CacheKey,
    HashCache,
    MetadataCache,
    MetadataCacheKey,
    _db,
)
from winnow.models.media import MEDIA_METADATA_SCHEMA_VERSION, MediaMetadata

SAMPLE = MediaMetadata(width=10, height=5, captured_at=datetime(2024, 3, 1, 12, 0))


@pytest.fixture
def cache(tmp_path: Path) -> MetadataCache:
    """Return a metadata cache backed by a temporary on-disk database.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        An open metadata cache instance.
    """
    return MetadataCache(db_path=tmp_path / "cache.db")


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """Return a small media file to key cache entries on.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path of the written file.
    """
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"pixels")
    return path


def _table_names(db_path: Path) -> set[str]:
    """Return the names of every table in the database at ``db_path``.

    Args:
        db_path: SQLite database file.

    Returns:
        Table names from ``sqlite_master``.
    """
    with sqlite3.connect(db_path) as raw:
        rows = raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    return {row[0] for row in rows}


def _row_count(db_path: Path) -> int:
    """Return ``COUNT(*)`` of ``metadata_cache`` at ``db_path``.

    Args:
        db_path: SQLite database file.

    Returns:
        Number of metadata rows.
    """
    with sqlite3.connect(db_path) as raw:
        return int(raw.execute("SELECT COUNT(*) FROM metadata_cache").fetchone()[0])


def _overwrite_row(db_path: Path, *, path: Path, column: str, value: object) -> None:
    """Set ``column`` to ``value`` on the metadata row for ``path`` via raw SQL.

    Args:
        db_path: SQLite database file.
        path: File whose (resolved) path identifies the row.
        column: Column to overwrite (``schema_version`` or ``payload``).
        value: New value.
    """
    with sqlite3.connect(db_path) as raw:
        # ``column`` is a test-controlled literal, never user input.
        raw.execute(
            f"UPDATE metadata_cache SET {column} = ? WHERE path = ?",  # nosec B608
            (value, str(path.resolve())),
        )


def test_hash_cache_provisions_both_tables(tmp_path: Path) -> None:
    """Opening only a HashCache creates the metadata table too."""
    db_path = tmp_path / "cache.db"

    with HashCache(db_path=db_path):
        pass

    assert_that(_table_names(db_path)).contains("hash_cache", "metadata_cache")


def test_metadata_cache_provisions_both_tables(tmp_path: Path) -> None:
    """Opening only a MetadataCache creates the hash table too."""
    db_path = tmp_path / "cache.db"

    with MetadataCache(db_path=db_path):
        pass

    assert_that(_table_names(db_path)).contains("hash_cache", "metadata_cache")


def test_legacy_unversioned_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """A pre-versioning cache.db keeps its hash rows and gains the new table."""
    db_path = tmp_path / "cache.db"
    with sqlite3.connect(db_path) as raw:
        raw.execute(_db.HASH_SCHEMA)
        raw.execute(
            "INSERT INTO hash_cache (path, algorithm, mtime, size, hash) "
            "VALUES ('/x', 'phash', 1.0, 1, 'abc')",
        )

    with MetadataCache(db_path=db_path):
        pass

    with sqlite3.connect(db_path) as raw:
        digest = raw.execute("SELECT hash FROM hash_cache").fetchone()[0]
    assert_that(digest).is_equal_to("abc")
    assert_that(_table_names(db_path)).contains("metadata_cache", "schema_version")


def test_get_on_missing_path_returns_none(cache: MetadataCache, tmp_path: Path) -> None:
    """A lookup for a file that does not exist is a miss, not an error."""
    result = cache.get(tmp_path / "missing.jpg")

    assert_that(result).is_none()
    assert_that(cache.stats().misses).is_equal_to(1)


def test_put_on_missing_path_raises(cache: MetadataCache, tmp_path: Path) -> None:
    """Writing metadata for a file that does not exist raises CacheError."""
    with pytest.raises(CacheError):
        cache.put(tmp_path / "missing.jpg", SAMPLE)


def test_miss_then_put_then_hit_round_trips(cache: MetadataCache, media: Path) -> None:
    """A put after a miss makes the next get return an equal MediaMetadata."""
    assert_that(cache.get(media)).is_none()

    cache.put(media, SAMPLE)
    result = cache.get(media)

    assert_that(result).is_equal_to(SAMPLE)
    assert_that(SAMPLE.captured_at).is_equal_to(datetime(2024, 3, 1, 12, 0))


def test_mtime_change_invalidates(cache: MetadataCache, media: Path) -> None:
    """Touching the file's mtime turns a cached entry into a miss."""
    cache.put(media, SAMPLE)
    current = media.stat().st_mtime
    os.utime(media, (current, current + 5))

    assert_that(cache.get(media)).is_none()


def test_size_change_invalidates(cache: MetadataCache, media: Path) -> None:
    """Growing the file turns a cached entry into a miss."""
    cache.put(media, SAMPLE)
    original_mtime = media.stat().st_mtime
    with media.open("ab") as handle:
        handle.write(b"more")
    os.utime(media, (original_mtime, original_mtime))

    assert_that(cache.get(media)).is_none()


def test_stale_schema_version_is_miss_and_deleted(
    cache: MetadataCache,
    media: Path,
    tmp_path: Path,
) -> None:
    """A row written under a different schema version is dropped on read."""
    cache.put(media, SAMPLE)
    _overwrite_row(
        tmp_path / "cache.db",
        path=media,
        column="schema_version",
        value=MEDIA_METADATA_SCHEMA_VERSION + 1,
    )

    assert_that(cache.get(media)).is_none()
    assert_that(_row_count(tmp_path / "cache.db")).is_equal_to(0)


def test_corrupt_payload_is_miss_and_deleted(
    cache: MetadataCache,
    media: Path,
    tmp_path: Path,
) -> None:
    """A payload that does not validate is dropped on read without raising."""
    cache.put(media, SAMPLE)
    _overwrite_row(
        tmp_path / "cache.db",
        path=media,
        column="payload",
        value="not json",
    )

    assert_that(cache.get(media)).is_none()
    assert_that(_row_count(tmp_path / "cache.db")).is_equal_to(0)


def test_prune_stale_removes_only_deleted_files(
    cache: MetadataCache,
    tmp_path: Path,
) -> None:
    """prune_stale drops rows for deleted files and keeps the rest."""
    kept = tmp_path / "kept.jpg"
    gone = tmp_path / "gone.jpg"
    kept.write_bytes(b"a")
    gone.write_bytes(b"b")
    cache.put(kept, SAMPLE)
    cache.put(gone, SAMPLE)
    gone.unlink()

    removed = cache.prune_stale()

    assert_that(removed).is_equal_to(1)
    assert_that(cache.get(kept)).is_equal_to(SAMPLE)
    assert_that(cache.stats().entry_count).is_equal_to(1)


def test_prune_stale_with_nothing_missing_returns_zero(
    cache: MetadataCache,
    media: Path,
) -> None:
    """prune_stale is a no-op when every cached file still exists."""
    cache.put(media, SAMPLE)

    assert_that(cache.prune_stale()).is_equal_to(0)


def test_clear_leaves_hash_cache_rows_intact(
    cache: MetadataCache,
    media: Path,
    tmp_path: Path,
) -> None:
    """clear empties metadata_cache only; hash_cache in the same file survives."""
    cache.put(media, SAMPLE)
    key = CacheKey.from_file(media, "phash")
    with HashCache(db_path=tmp_path / "cache.db") as hashes:
        hashes.set(key, "abcd")

    cache.clear()

    assert_that(cache.stats().entry_count).is_equal_to(0)
    with HashCache(db_path=tmp_path / "cache.db") as hashes:
        assert_that(hashes.get(key)).is_equal_to("abcd")


def test_stats_reports_hits_misses_and_entry_count(
    cache: MetadataCache,
    media: Path,
) -> None:
    """stats() reflects one miss, one hit, and one stored row."""
    cache.get(media)
    cache.put(media, SAMPLE)
    cache.get(media)

    stats = cache.stats()

    assert_that(stats.hits).is_equal_to(1)
    assert_that(stats.misses).is_equal_to(1)
    assert_that(stats.entry_count).is_equal_to(1)
    assert_that(stats.size_bytes).is_greater_than(0)


def test_metadata_cache_key_from_missing_file_raises(tmp_path: Path) -> None:
    """MetadataCacheKey.from_file raises CacheError when stat() fails."""
    with pytest.raises(CacheError) as excinfo:
        MetadataCacheKey.from_file(tmp_path / "missing")

    assert_that(excinfo.value.context.operation).is_equal_to(
        "metadata_cache_key.from_file",
    )


def test_metadata_cache_key_resolves_path(media: Path) -> None:
    """MetadataCacheKey.from_file records the resolved path, mtime and size."""
    stat = media.stat()

    key = MetadataCacheKey.from_file(str(media))

    assert_that(key.path).is_equal_to(media.resolve())
    assert_that(key.mtime).is_equal_to(stat.st_mtime)
    assert_that(key.size).is_equal_to(stat.st_size)


def test_in_memory_database_round_trips(media: Path) -> None:
    """A ":memory:" cache stores and returns metadata with size_bytes == 0."""
    with MetadataCache(":memory:") as cache:
        cache.put(media, SAMPLE)

        assert_that(cache.get(media)).is_equal_to(SAMPLE)
        assert_that(cache.stats().size_bytes).is_equal_to(0)


def test_operations_after_close_raise_cache_error(
    cache: MetadataCache,
    media: Path,
) -> None:
    """Every SQL-backed method wraps sqlite3 errors as CacheError."""
    cache.put(media, SAMPLE)
    cache.close()

    with pytest.raises(CacheError) as get_error:
        cache.get(media)
    with pytest.raises(CacheError) as put_error:
        cache.put(media, SAMPLE)
    with pytest.raises(CacheError) as prune_error:
        cache.prune_stale()
    with pytest.raises(CacheError) as clear_error:
        cache.clear()
    with pytest.raises(CacheError) as stats_error:
        cache.stats()

    assert_that(get_error.value.context.operation).is_equal_to("metadata_cache.get")
    assert_that(put_error.value.context.operation).is_equal_to("metadata_cache.put")
    assert_that(prune_error.value.context.operation).is_equal_to(
        "metadata_cache.prune_stale",
    )
    assert_that(clear_error.value.context.operation).is_equal_to(
        "metadata_cache.clear",
    )
    assert_that(stats_error.value.context.operation).is_equal_to(
        "metadata_cache.stats",
    )


def test_initialize_schema_failure_wraps_storage_error(tmp_path: Path) -> None:
    """A database newer than this build is rejected as CacheError."""
    db_path = tmp_path / "cache.db"
    with sqlite3.connect(db_path) as raw:
        raw.execute(_db.SCHEMA_VERSION_TABLE)
        raw.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (_db.CACHE_SCHEMA_VERSION + 1,),
        )

    with pytest.raises(CacheError) as excinfo:
        MetadataCache(db_path=db_path)

    assert_that(excinfo.value.context.operation).is_equal_to("cache.initialize")
