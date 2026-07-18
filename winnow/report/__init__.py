"""SQLite report schema and persistence layer.

This package provides the versioned SQLite v2 report schema and a typed
persistence layer (:class:`ReportDatabase`) for recording winnow report runs,
discovered media files, duplicate groups, and filesystem operations, with an
FTS5 full-text index over media file paths, filenames, and metadata.
"""

from __future__ import annotations

from winnow.report.database import ReportDatabase
from winnow.report.records import (
    DuplicateGroupRecord,
    MediaFileRecord,
    OperationRecord,
    ReportRun,
)
from winnow.report.schema import (
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    TERMINAL_RUN_STATUSES,
    RunStatus,
)

__all__ = [
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "TERMINAL_RUN_STATUSES",
    "DuplicateGroupRecord",
    "MediaFileRecord",
    "OperationRecord",
    "ReportDatabase",
    "ReportRun",
    "RunStatus",
]
