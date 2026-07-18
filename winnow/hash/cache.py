"""SQLite-backed content-addressable perceptual-hash cache.

The cache maps ``(path, mtime, size, algorithm)`` keys to perceptual hash
digests. Entries are invalidated automatically when a file's modification time
or size changes, and stale rows for deleted files can be pruned on demand.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import Self

from winnow.exceptions import CacheError
from winnow.hash.cache_entry import CacheEntry
from winnow.hash.cache_key import CacheKey
from winnow.hash.cache_stats import CacheStats

_IN_MEMORY = ":memory:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hash_cache (
    path TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (path, algorithm)
)
"""


def _default_db_path() -> Path:
    """Return the default on-disk location for the cache database.

    Returns:
        Path to ``cache.db`` under the user's XDG cache directory.
    """
    return Path.home() / ".cache" / "winnow" / "cache.db"


class HashCache:
    """Persistent perceptual-hash cache backed by SQLite.

    The database path is injectable so tests can use a temporary file or an
    in-memory database. Hard failures surface as :class:`CacheError`.

    Args:
        db_path: Location of the SQLite database. Defaults to
            ``~/.cache/winnow/cache.db``. Pass ``":memory:"`` for a transient
            in-memory database.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        """Open the cache database, creating its schema if necessary."""
        self._in_memory = db_path == _IN_MEMORY
        if db_path is None:
            self._db_path: Path = _default_db_path()
        else:
            self._db_path = Path(db_path)
        self._hits = 0
        self._misses = 0
        self._connection = self._connect()
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection, creating parent directories as needed.

        Returns:
            An open SQLite connection.

        Raises:
            CacheError: If the database or its parent directory is unavailable.
        """
        target = _IN_MEMORY if self._in_memory else str(self._db_path)
        try:
            if not self._in_memory:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
            return sqlite3.connect(target)
        except (OSError, sqlite3.Error) as exc:
            raise CacheError(
                "Unable to open hash cache database",
                operation="cache.connect",
                file_path=self._db_path,
            ) from exc

    def _initialize_schema(self) -> None:
        """Create the cache table when it does not already exist.

        Raises:
            CacheError: If the schema cannot be created.
        """
        try:
            with self._connection:
                self._connection.execute(_SCHEMA)
        except sqlite3.Error as exc:
            raise CacheError(
                "Unable to initialize hash cache schema",
                operation="cache.initialize",
                file_path=self._db_path,
            ) from exc

    def get(self, key: CacheKey) -> str | None:
        """Look up the digest stored for a key, if the entry is still valid.

        A lookup misses when no row exists or when the stored ``mtime`` or
        ``size`` differ from the key, which reflects an invalidated entry.

        Args:
            key: Cache key identifying the file, metadata, and algorithm.

        Returns:
            The stored digest, or ``None`` on a miss.

        Raises:
            CacheError: If the lookup query fails.
        """
        digest = self._lookup(key)
        if digest is None:
            self._misses += 1
        else:
            self._hits += 1
        return digest

    def get_many(self, keys: Iterable[CacheKey]) -> dict[CacheKey, str]:
        """Look up digests for several keys, recording hits and misses.

        Args:
            keys: Cache keys to resolve.

        Returns:
            A mapping of each hitting key to its stored digest. Missing keys are
            omitted.

        Raises:
            CacheError: If any lookup query fails.
        """
        results: dict[CacheKey, str] = {}
        for key in keys:
            digest = self._lookup(key)
            if digest is None:
                self._misses += 1
            else:
                self._hits += 1
                results[key] = digest
        return results

    def _lookup(self, key: CacheKey) -> str | None:
        """Return the stored digest for a key without touching counters.

        Args:
            key: Cache key to resolve.

        Returns:
            The stored digest, or ``None`` when no valid entry exists.

        Raises:
            CacheError: If the lookup query fails.
        """
        try:
            cursor = self._connection.execute(
                "SELECT hash FROM hash_cache "
                "WHERE path = ? AND algorithm = ? AND mtime = ? AND size = ?",
                (
                    str(key.path),
                    str(key.algorithm),
                    key.mtime,
                    key.size,
                ),
            )
            row = cursor.fetchone()
        except sqlite3.Error as exc:
            raise CacheError(
                "Hash cache lookup failed",
                operation="cache.get",
                file_path=key.path,
            ) from exc
        if row is None:
            return None
        digest: str = row[0]
        return digest

    def set(self, key: CacheKey, digest: str) -> None:
        """Store or replace the digest for a key.

        Because a file has one row per algorithm, writing a new key for the same
        ``(path, algorithm)`` overwrites any prior entry, invalidating stale
        metadata.

        Args:
            key: Cache key identifying the file, metadata, and algorithm.
            digest: Perceptual hash digest to persist.

        Raises:
            CacheError: If the write fails.
        """
        self.set_many([CacheEntry(key=key, digest=digest)])

    def set_many(self, entries: Iterable[CacheEntry]) -> None:
        """Store or replace digests for several entries in one transaction.

        Args:
            entries: Cache entries pairing keys with digests.

        Raises:
            CacheError: If the write fails.
        """
        rows = [
            (
                str(entry.key.path),
                str(entry.key.algorithm),
                entry.key.mtime,
                entry.key.size,
                entry.digest,
            )
            for entry in entries
        ]
        if not rows:
            return
        try:
            with self._connection:
                self._connection.executemany(
                    "INSERT OR REPLACE INTO hash_cache "
                    "(path, algorithm, mtime, size, hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            raise CacheError(
                "Hash cache write failed",
                operation="cache.set",
                details={"entry_count": len(rows)},
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
                "SELECT DISTINCT path FROM hash_cache",
            )
            paths = [row[0] for row in cursor.fetchall()]
            missing = [path for path in paths if not Path(path).exists()]
            if not missing:
                return 0
            with self._connection:
                deleted = 0
                for path in missing:
                    result = self._connection.execute(
                        "DELETE FROM hash_cache WHERE path = ?",
                        (path,),
                    )
                    deleted += result.rowcount
            return deleted
        except sqlite3.Error as exc:
            raise CacheError(
                "Hash cache prune failed",
                operation="cache.prune_stale",
                file_path=self._db_path,
            ) from exc

    def clear(self) -> None:
        """Remove every entry from the cache without resetting counters.

        Raises:
            CacheError: If the delete fails.
        """
        try:
            with self._connection:
                self._connection.execute("DELETE FROM hash_cache")
        except sqlite3.Error as exc:
            raise CacheError(
                "Hash cache clear failed",
                operation="cache.clear",
                file_path=self._db_path,
            ) from exc

    def stats(self) -> CacheStats:
        """Return current hit/miss counters, entry count, and database size.

        Returns:
            A snapshot of cache activity and on-disk size.

        Raises:
            CacheError: If the entry count query fails.
        """
        try:
            cursor = self._connection.execute(
                "SELECT COUNT(*) FROM hash_cache",
            )
            entry_count: int = cursor.fetchone()[0]
        except sqlite3.Error as exc:
            raise CacheError(
                "Hash cache stats query failed",
                operation="cache.stats",
                file_path=self._db_path,
            ) from exc
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            entry_count=entry_count,
            size_bytes=self._database_size_bytes(),
        )

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
