"""SQLite-backed cache of extracted :class:`MediaMetadata`.

The cache maps ``(path, mtime, size)`` keys to a JSON-serialized
:class:`~winnow.models.media.MediaMetadata` tagged with the
:data:`~winnow.models.media.MEDIA_METADATA_SCHEMA_VERSION` it was written
under. Entries are invalidated when a file's modification time or size
changes, when the model schema version moves on, or when the stored payload
no longer validates. Eviction is lazy: a stale row is deleted when a live
:meth:`MetadataCache.get` misses on it, and rows for deleted files go via
:meth:`MetadataCache.prune_stale`. ``entry_count`` is the raw table size, so
stale rows that have not been read since the file changed still count.

The store shares ``cache.db`` with :class:`winnow.hash.cache.HashCache` and is
a pure key/value surface: the "check cache, extract on miss" policy belongs to
the pipeline's Metadata step (#55). Connection and query plumbing lives in the
private sibling module :mod:`winnow.hash._db`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import ValidationError

from winnow.exceptions import CacheError
from winnow.hash import _db
from winnow.hash.cache_stats import CacheStats
from winnow.hash.metadata_cache_key import MetadataCacheKey
from winnow.models.media import MEDIA_METADATA_SCHEMA_VERSION, MediaMetadata


class MetadataCache:
    """Persistent media-metadata cache backed by SQLite.

    Hard failures surface as :class:`CacheError`.

    Args:
        db_path: Location of the SQLite database. Defaults to ``cache.db``
            under the :class:`CacheSettings` default cache directory (shared
            with :class:`~winnow.hash.cache.HashCache`). Pass ``":memory:"``
            for a transient in-memory database.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        """Open the cache database, creating its schema if necessary."""
        self._in_memory = db_path == _db.IN_MEMORY
        if db_path is None:
            self._db_path: Path = _db.default_db_path()
        else:
            self._db_path = Path(db_path)
        self._hits = 0
        self._misses = 0
        self._connection = _db.open_database(
            db_path=self._db_path,
            in_memory=self._in_memory,
        )

    def get(
        self,
        path: Path,
        *,
        key: MetadataCacheKey | None = None,
    ) -> MediaMetadata | None:
        """Return the cached metadata for ``path`` if the entry is still valid.

        A hit requires a row matching the key's ``mtime`` and ``size``,
        written under the current :data:`MEDIA_METADATA_SCHEMA_VERSION`,
        whose payload validates as :class:`MediaMetadata`. A row with a stale
        schema version or an invalid payload is deleted in the same call. A
        row whose ``mtime``/``size`` differ is deleted only when ``key`` was
        read from the live file here; a caller-supplied snapshot that does not
        match is a plain miss, since the row may still describe the file as it
        is now. An unreadable file is a miss, not an error.

        Args:
            path: Filesystem path of the media file.
            key: File-state snapshot to look up under. Defaults to the file's
                current ``mtime`` and ``size``.

        Returns:
            The cached metadata, or ``None`` on a miss.

        Raises:
            CacheError: If the lookup or stale-row deletion fails.
        """
        key_is_live = key is None
        if key is None:
            try:
                key = MetadataCacheKey.from_file(path)
            except CacheError:
                self._misses += 1
                return None
        row = _db.lookup_metadata_row(connection=self._connection, key=key)
        metadata = None
        if row is not None:
            metadata = self._resolve_row(row, key=key, evict_on_mismatch=key_is_live)
        if metadata is None:
            self._misses += 1
            return None
        self._hits += 1
        return metadata

    def put(
        self,
        path: Path,
        metadata: MediaMetadata,
        *,
        key: MetadataCacheKey | None = None,
    ) -> None:
        """Store or replace the metadata for ``path``.

        Degraded-but-valid results (an empty :class:`MediaMetadata` for a video
        without ``ffprobe``) are stored like any other value.

        Args:
            path: Filesystem path of the media file.
            metadata: Extracted metadata to persist.
            key: File-state snapshot the metadata was extracted from. When
                omitted the file is stat'ed at write time, which is only
                correct if the file has not changed since extraction; a file
                rewritten in between would be recorded as current with stale
                metadata. Callers that extract and store in separate steps
                (the pipeline's Metadata step, #55) should take the key before
                extraction and pass it here.

        Raises:
            CacheError: If the file metadata cannot be read or the write fails.
        """
        if key is None:
            key = MetadataCacheKey.from_file(path)
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT OR REPLACE INTO metadata_cache "
                    "(path, mtime, size, schema_version, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(key.path),
                        key.mtime,
                        key.size,
                        MEDIA_METADATA_SCHEMA_VERSION,
                        metadata.model_dump_json(),
                    ),
                )
        except sqlite3.Error as exc:
            raise CacheError(
                "Metadata cache write failed",
                operation="metadata_cache.put",
                file_path=self._db_path,
            ) from exc

    def prune_stale(self) -> int:
        """Delete cached entries whose source files no longer exist.

        Returns:
            The number of rows removed.

        Raises:
            CacheError: If the scan or deletion fails.
        """
        try:
            cursor = self._connection.execute(
                "SELECT DISTINCT path FROM metadata_cache",
            )
            paths = [row[0] for row in cursor.fetchall()]
            missing = [path for path in paths if not Path(path).exists()]
            if not missing:
                return 0
            deleted = 0
            with self._connection:
                for chunk in _db.chunked(missing, _db.MAX_SQL_VARIABLES):
                    placeholders = ", ".join("?" for _ in chunk)
                    # Only "?" markers are interpolated; values are bound.
                    query = (
                        "DELETE FROM metadata_cache "
                        f"WHERE path IN ({placeholders})"  # nosec B608
                    )
                    result = self._connection.execute(query, tuple(chunk))
                    deleted += result.rowcount
            return deleted
        except sqlite3.Error as exc:
            raise CacheError(
                "Metadata cache prune failed",
                operation="metadata_cache.prune_stale",
                file_path=self._db_path,
            ) from exc

    def clear(self) -> None:
        """Remove every metadata entry, leaving ``hash_cache`` and counters alone.

        Raises:
            CacheError: If the delete fails.
        """
        try:
            with self._connection:
                self._connection.execute("DELETE FROM metadata_cache")
        except sqlite3.Error as exc:
            raise CacheError(
                "Metadata cache clear failed",
                operation="metadata_cache.clear",
                file_path=self._db_path,
            ) from exc

    def stats(self) -> CacheStats:
        """Return current hit/miss counters, entry count, and database size.

        ``size_bytes`` is the size of the whole shared ``cache.db`` file, which
        also holds the hash cache; it is ``0`` for an in-memory database.

        Returns:
            A snapshot of cache activity and on-disk size.

        Raises:
            CacheError: If the entry count query fails.
        """
        try:
            cursor = self._connection.execute(
                "SELECT COUNT(*) FROM metadata_cache",
            )
            entry_count: int = cursor.fetchone()[0]
        except sqlite3.Error as exc:
            raise CacheError(
                "Metadata cache stats query failed",
                operation="metadata_cache.stats",
                file_path=self._db_path,
            ) from exc
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            entry_count=entry_count,
            size_bytes=self._database_size_bytes(),
        )

    def close(self) -> None:
        """Close the underlying database connection."""
        self._connection.close()

    def __enter__(self) -> Self:
        """Enter a context manager scope.

        Returns:
            This cache instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection when leaving a context manager scope."""
        self.close()

    def _resolve_row(
        self,
        row: tuple[float, int, int, str],
        *,
        key: MetadataCacheKey,
        evict_on_mismatch: bool,
    ) -> MediaMetadata | None:
        """Validate a stored row against ``key``, evicting it when appropriate.

        Args:
            row: ``(mtime, size, schema_version, payload)`` from the database.
            key: File state the row must match to count as a hit.
            evict_on_mismatch: Whether a ``mtime``/``size`` mismatch should
                delete the row. True when ``key`` reflects the live file, so
                the row is known to be stale; false for a caller-supplied
                snapshot, which says nothing about the file's current state.

        Returns:
            The decoded metadata on a hit, or ``None`` on a miss.

        Raises:
            CacheError: If a stale-row deletion fails.
        """
        metadata = self._decode_row(row)
        if metadata is None:
            self._delete_row(key)
            return None
        mtime, size, _schema_version, _payload = row
        if (mtime, size) != (key.mtime, key.size):
            if evict_on_mismatch:
                self._delete_row(key)
            return None
        return metadata

    @staticmethod
    def _decode_row(row: tuple[float, int, int, str]) -> MediaMetadata | None:
        """Turn a stored row into metadata when it is well-formed.

        File-state matching is the caller's concern; this only checks that
        the row was written under the current schema and still validates.

        Args:
            row: ``(mtime, size, schema_version, payload)`` from the database.

        Returns:
            The decoded metadata, or ``None`` when the schema version is not
            current or the payload does not validate.
        """
        _mtime, _size, schema_version, payload = row
        if schema_version != MEDIA_METADATA_SCHEMA_VERSION:
            return None
        try:
            return MediaMetadata.model_validate_json(payload)
        except ValidationError:
            return None

    def _delete_row(self, key: MetadataCacheKey) -> None:
        """Delete the stale or corrupt row stored for ``key.path``.

        Args:
            key: Key whose path identifies the row to remove.

        Raises:
            CacheError: If the delete fails.
        """
        try:
            with self._connection:
                self._connection.execute(
                    "DELETE FROM metadata_cache WHERE path = ?",
                    (str(key.path),),
                )
        except sqlite3.Error as exc:
            raise CacheError(
                "Metadata cache stale-row delete failed",
                operation="metadata_cache.get",
                file_path=self._db_path,
            ) from exc

    def _database_size_bytes(self) -> int:
        """Return the on-disk size of the database file in bytes.

        Returns:
            File size in bytes, or ``0`` for an in-memory or absent database.
        """
        if self._in_memory:
            return 0
        try:
            return self._db_path.stat().st_size
        except OSError:
            return 0
