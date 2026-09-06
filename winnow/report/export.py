"""Export one pipeline run into the report database.

:func:`export_run` is the single writer that turns the facts of one
organize/scan run (:class:`RunExport`) into rows of the SQLite v2 report
schema. Every statement runs inside one
:meth:`~winnow.report._connection.ConnectionManager.transaction` so the report
is always whole or absent, and re-exporting with an existing ``run_id``
replaces the earlier rows instead of double-counting them.

The row mapping implemented here is the contract consumed by the reporting
pipeline step and the query/import commands; in particular ``creation_date``
is always stored as UTC ``YYYY-MM-DDTHH:MM:SSZ``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from winnow.exceptions import ReportError
from winnow.fs.operation_log import OperationLog
from winnow.models.duplicates import DuplicateGroup
from winnow.models.media import MediaFile
from winnow.report._export_rows import (
    _insert_groups,
    _insert_media_files,
    _insert_operations,
    _insert_run,
)
from winnow.report.database import ReportDatabase
from winnow.report.schema import RunStatus

MSG_DUPLICATE_PATH = "duplicate path in export"
MSG_UNKNOWN_GROUP_MEMBER = "duplicate group member is not in export files"
MSG_REPEATED_GROUP_MEMBER = "duplicate group member appears in more than one group"

_OPERATION = "export_run"


@dataclass(frozen=True, slots=True)
class RunExport:
    """Facts of one pipeline run to persist in the report database.

    Args:
        root_path: Root directory the run scanned.
        files: Every media file discovered by the run.
        groups: Duplicate groups detected among ``files``.
        operations: Filesystem operations applied during the run.
        status: Lifecycle status to record for the run.
        notes: Free-form provenance annotation (for example a saga session id).
        content_hashes: Content hash per file path, when computed.
        quality_scores: Quality score per file path, when computed.
    """

    root_path: Path
    files: Sequence[MediaFile]
    groups: Sequence[DuplicateGroup] = ()
    operations: Sequence[OperationLog] = ()
    status: RunStatus = RunStatus.COMPLETED
    notes: str | None = None
    content_hashes: Mapping[Path, str] = field(default_factory=dict)
    quality_scores: Mapping[Path, float] = field(default_factory=dict)


def _validate(export: RunExport) -> None:
    """Reject exports that cannot be mapped onto the schema.

    Args:
        export: Run facts to validate.

    Raises:
        ReportError: If ``files`` repeats a path, a group references a path
            that is not in ``files``, or a path belongs to more than one group.
    """
    seen: set[str] = set()
    grouped: set[str] = set()
    for media in export.files:
        key = str(media.path)
        if key in seen:
            raise ReportError(
                MSG_DUPLICATE_PATH,
                operation=_OPERATION,
                file_path=media.path,
            )
        seen.add(key)
    for group in export.groups:
        for member in group.files:
            key = str(member)
            if key not in seen:
                raise ReportError(
                    MSG_UNKNOWN_GROUP_MEMBER,
                    operation=_OPERATION,
                    file_path=member,
                    details={"group_number": group.group_number},
                )
            if key in grouped:
                raise ReportError(
                    MSG_REPEATED_GROUP_MEMBER,
                    operation=_OPERATION,
                    file_path=member,
                    details={"group_number": group.group_number},
                )
            grouped.add(key)


def export_run(
    database: ReportDatabase,
    export: RunExport,
    *,
    run_id: int | None = None,
) -> int:
    """Persist one run atomically in the report database.

    With ``run_id`` unset a new ``report_runs`` row is created. With
    ``run_id`` set the existing run and every row that cascades from it are
    deleted and re-inserted under the same identifier, so repeated exports of
    the same run never double-count.

    Args:
        database: Connected report database to write to.
        export: Run facts to persist.
        run_id: Existing run to replace, or ``None`` to create a new run.

    Returns:
        The identifier of the exported run.

    Raises:
        ReportError: If the export is invalid, ``run_id`` names a run that
            does not exist, or any statement fails; nothing is written in any
            of these cases.
    """
    _validate(export)
    with database.transaction() as cursor:
        inserted = _insert_run(cursor, export=export, run_id=run_id)
        file_ids = _insert_media_files(cursor, export=export, run_id=inserted)
        _insert_groups(cursor, export=export, run_id=inserted, file_ids=file_ids)
        _insert_operations(
            cursor,
            export=export,
            run_id=inserted,
            file_ids=file_ids,
        )
    return inserted


__all__ = ["RunExport", "export_run"]
