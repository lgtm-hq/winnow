"""SQLite connection and query plumbing for the hash and metadata caches.

This private module owns the low-level database concerns shared by
:class:`winnow.hash.cache.HashCache` and
:class:`winnow.hash.metadata_cache.MetadataCache`: opening connections,
provisioning the schema, batching ``IN`` clauses under SQLite's variable
limit, and fetching rows. Both caches live in one ``cache.db`` file; opening
either one provisions both tables through
:func:`winnow.storage.apply_schema`, so #48's prune/clear surface has one
file to manage. The public cache classes compose these helpers and keep the
hit/miss bookkeeping.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

from winnow.exceptions import CacheError, StorageError
from winnow.hash.cache_key import CacheKey
from winnow.hash.metadata_cache_key import MetadataCacheKey
from winnow.models.config import CacheSettings
from winnow.storage import Migration, apply_schema

IN_MEMORY = ":memory:"

CACHE_DB_FILENAME: Final[str] = "cache.db"

# Keep parameter counts comfortably below SQLite's compiled variable limit
# (``SQLITE_MAX_VARIABLE_NUMBER``, historically 999) so batched IN clauses stay
# valid regardless of the linked SQLite build.
MAX_SQL_VARIABLES = 900

StoredRow = tuple[str, str, float, int]

CACHE_SCHEMA_VERSION: Final[int] = 1
"""Current ``cache.db`` schema version persisted in ``schema_version``.

Databases created before versioning carry no ``schema_version`` table and
therefore read as version ``0``; the baseline below is idempotent
(``CREATE TABLE IF NOT EXISTS``), so they are brought current without data
loss. Bumping this constant requires updating :data:`SCHEMA_STATEMENTS`
**and** appending a :class:`~winnow.storage.Migration` to :data:`MIGRATIONS`.
"""

SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

HASH_SCHEMA = """
CREATE TABLE IF NOT EXISTS hash_cache (
    path TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (path, algorithm)
)
"""

METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata_cache (
    path TEXT NOT NULL PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
)
"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    SCHEMA_VERSION_TABLE,
    HASH_SCHEMA,
    METADATA_SCHEMA,
)
"""Ordered DDL that provisions the full ``cache.db`` schema."""

MIGRATIONS: tuple[Migration, ...] = ()
"""Upgrade steps from older versioned schemas; empty at version 1."""


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
    """Provision or migrate both cache tables through :func:`apply_schema`.

    Args:
        connection: Open connection to run the schema statements on.
        db_path: Database location, used for error reporting.

    Raises:
        CacheError: If the schema cannot be created or migrated.
    """
    try:
        apply_schema(
            connection,
            baseline=SCHEMA_STATEMENTS,
            migrations=MIGRATIONS,
            target_version=CACHE_SCHEMA_VERSION,
        )
    except StorageError as exc:
        raise CacheError(
            "Unable to initialize cache schema",
            operation="cache.initialize",
            file_path=db_path,
            details=exc.context.details,
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


def lookup_metadata_row(
    *,
    connection: sqlite3.Connection,
    key: MetadataCacheKey,
) -> tuple[int, str] | None:
    """Return the stored ``(schema_version, payload)`` for a key, or ``None``.

    Args:
        connection: Open connection to query.
        key: Metadata cache key to resolve.

    Returns:
        The stored schema version and JSON payload, or ``None`` when no row
        matches the key's ``path``, ``mtime`` and ``size`` exactly.

    Raises:
        CacheError: If the lookup query fails.
    """
    try:
        cursor = connection.execute(
            "SELECT schema_version, payload FROM metadata_cache "
            "WHERE path = ? AND mtime = ? AND size = ?",
            (str(key.path), key.mtime, key.size),
        )
        row = cursor.fetchone()
    except sqlite3.Error as exc:
        raise CacheError(
            "Metadata cache lookup failed",
            operation="metadata_cache.get",
            file_path=key.path,
        ) from exc
    if row is None:
        return None
    schema_version: int = row[0]
    payload: str = row[1]
    return schema_version, payload
