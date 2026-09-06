"""SQLite connection and query plumbing for the perceptual-hash cache.

This private module owns the low-level database concerns shared by
:class:`winnow.hash.cache.HashCache`: opening connections, creating the
schema, batching ``IN`` clauses under SQLite's variable limit, and fetching
rows. The public cache class in :mod:`winnow.hash.cache` composes these
helpers and keeps the hit/miss bookkeeping.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

from winnow.exceptions import CacheError
from winnow.hash.cache_key import CacheKey
from winnow.models.config import CacheSettings

IN_MEMORY = ":memory:"

CACHE_DB_FILENAME: Final[str] = "cache.db"

# Keep parameter counts comfortably below SQLite's compiled variable limit
# (``SQLITE_MAX_VARIABLE_NUMBER``, historically 999) so batched IN clauses stay
# valid regardless of the linked SQLite build.
MAX_SQL_VARIABLES = 900

StoredRow = tuple[str, str, float, int]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hash_cache (
    path TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (path, algorithm)
)
"""


def default_db_path(settings: CacheSettings | None = None) -> Path:
    """Return the on-disk location of the cache database for ``settings``.

    Args:
        settings: Cache settings whose ``directory`` hosts the database. When
            ``None``, the :class:`CacheSettings` defaults are used so the
            cache-directory default is defined exactly once.

    Returns:
        ``settings.directory / "cache.db"``.
    """
    return (settings or CacheSettings()).directory / CACHE_DB_FILENAME


def chunked(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Yield successive slices of ``items`` no longer than ``size``.

    Args:
        items: Sequence to split into batches.
        size: Maximum length of each yielded slice.

    Yields:
        Consecutive, non-overlapping slices covering ``items``.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


def connect(*, db_path: Path, in_memory: bool) -> sqlite3.Connection:
    """Open a SQLite connection, creating parent directories as needed.

    On-disk databases are switched to WAL journaling so concurrent workers
    sharing the default database do not block one another; in-memory databases
    retain SQLite's default journal, where WAL does not apply.

    Args:
        db_path: Location of the on-disk database file. Ignored when
            ``in_memory`` is true, apart from error reporting.
        in_memory: Whether to open a transient in-memory database.

    Returns:
        An open SQLite connection.

    Raises:
        CacheError: If the database or its parent directory is unavailable.
    """
    target = IN_MEMORY if in_memory else str(db_path)
    connection: sqlite3.Connection | None = None
    try:
        if not in_memory:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target)
        if not in_memory:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise CacheError(
            "Unable to open hash cache database",
            operation="cache.connect",
            file_path=db_path,
        ) from exc


def initialize_schema(
    *,
    connection: sqlite3.Connection,
    db_path: Path,
) -> None:
    """Create the cache table when it does not already exist.

    Args:
        connection: Open connection to run the schema statement on.
        db_path: Database location, used for error reporting.

    Raises:
        CacheError: If the schema cannot be created.
    """
    try:
        with connection:
            connection.execute(SCHEMA)
    except sqlite3.Error as exc:
        raise CacheError(
            "Unable to initialize hash cache schema",
            operation="cache.initialize",
            file_path=db_path,
        ) from exc


def fetch_rows_for_paths(
    *,
    connection: sqlite3.Connection,
    paths: set[str],
) -> dict[StoredRow, str]:
    """Fetch every stored row whose path is in ``paths``.

    Args:
        connection: Open connection to query.
        paths: Distinct path strings to fetch rows for.

    Returns:
        A mapping of each stored ``(path, algorithm, mtime, size)`` identity
        to its digest.

    Raises:
        CacheError: If a batch lookup query fails.
    """
    stored: dict[StoredRow, str] = {}
    ordered = sorted(paths)
    try:
        for chunk in chunked(ordered, MAX_SQL_VARIABLES):
            placeholders = ", ".join("?" for _ in chunk)
            # The interpolated fragment is only "?" placeholder markers; all
            # values are bound as parameters, so injection is not possible.
            query = (
                "SELECT path, algorithm, mtime, size, hash "
                "FROM hash_cache "
                f"WHERE path IN ({placeholders})"  # nosec B608
            )
            cursor = connection.execute(query, tuple(chunk))
            for path, algorithm, mtime, size, digest in cursor.fetchall():
                stored[(path, algorithm, mtime, size)] = digest
    except sqlite3.Error as exc:
        raise CacheError(
            "Hash cache batch lookup failed",
            operation="cache.get_many",
        ) from exc
    return stored


def lookup_digest(
    *,
    connection: sqlite3.Connection,
    key: CacheKey,
) -> str | None:
    """Return the stored digest for a key, or ``None`` on a miss.

    Args:
        connection: Open connection to query.
        key: Cache key to resolve.

    Returns:
        The stored digest, or ``None`` when no valid entry exists.

    Raises:
        CacheError: If the lookup query fails.
    """
    try:
        cursor = connection.execute(
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
