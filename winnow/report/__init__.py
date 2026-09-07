"""SQLite report schema and persistence layer.

This package provides the versioned SQLite v2 report schema and a typed
persistence layer (:class:`ReportDatabase`) for recording winnow report runs,
discovered media files, duplicate groups, and filesystem operations, with an
FTS5 full-text index over media file paths, filenames, and metadata. The
filtered, sorted and paginated read queries shared by the CLI and the API live
in :mod:`winnow.report.queries`.
"""

from __future__ import annotations

from winnow.report.database import ReportDatabase
from winnow.report.export import RunExport, export_run
from winnow.report.queries import (
    MAX_PER_PAGE,
    DuplicateGroupWithMembers,
    DuplicateStatus,
    MediaFileFilter,
    MediaFileSort,
    Page,
    PageRequest,
    SortDirection,
    list_duplicate_groups_page,
    list_media_files_page,
    list_runs_page,
)
from winnow.report.records import (
    DuplicateGroupRecord,
    MediaFileRecord,
    OperationRecord,
    ReportRun,
)
from winnow.report.schema import (
    MIGRATIONS,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    TERMINAL_RUN_STATUSES,
    RunStatus,
)

__all__ = [
    "MAX_PER_PAGE",
    "MIGRATIONS",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "TERMINAL_RUN_STATUSES",
    "DuplicateGroupRecord",
    "DuplicateGroupWithMembers",
    "DuplicateStatus",
    "MediaFileFilter",
    "MediaFileRecord",
    "MediaFileSort",
    "OperationRecord",
    "Page",
    "PageRequest",
    "ReportDatabase",
    "ReportRun",
    "RunExport",
    "RunStatus",
    "SortDirection",
    "export_run",
    "list_duplicate_groups_page",
    "list_media_files_page",
    "list_runs_page",
]
