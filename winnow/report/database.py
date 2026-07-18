"""SQLite-backed report database with schema versioning and CRUD helpers.

:class:`ReportDatabase` wraps a stdlib :mod:`sqlite3` connection and provides
typed create/read/update/delete helpers for report runs, media files,
duplicate groups, and filesystem operations. It owns schema provisioning and
version tracking, and exposes a full-text search over media file paths,
filenames, and metadata backed by the FTS5 index defined in
:mod:`winnow.report.schema`.

The implementation is split per concern: connection and schema lifecycle live
in :mod:`winnow.report._connection`, and the per-entity CRUD helpers live in
:mod:`winnow.report._runs`, :mod:`winnow.report._groups`,
:mod:`winnow.report._media`, and :mod:`winnow.report._operations`. This module
composes them into the single public :class:`ReportDatabase` API.

The database path is injectable; the default ``":memory:"`` keeps a private
in-memory database alive for the lifetime of the instance, which is convenient
for tests and ephemeral reporting.
"""

from __future__ import annotations

from winnow.report._groups import GroupStore
from winnow.report._media import MediaStore
from winnow.report._operations import OperationStore
from winnow.report._runs import RunStore


class ReportDatabase(RunStore, GroupStore, MediaStore, OperationStore):
    """Persistence layer for winnow report data.

    Args:
        path: Filesystem path to the SQLite database, or ``":memory:"`` for a
            transient in-memory database. Defaults to ``":memory:"``.
    """


__all__ = ["ReportDatabase"]
