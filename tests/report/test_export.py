"""Tests for exporting a pipeline run into the report database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import ReportError
from winnow.fs.operation_log import OperationLog
from winnow.fs.operations import FileOperation
from winnow.models.duplicates import DuplicateGroup
from winnow.models.media import MediaFile, MediaMetadata, MediaType
from winnow.report import ReportDatabase, RunExport, RunStatus, export_run
from winnow.report import export as export_module
from winnow.report._export_rows import _format_creation_date, _metadata_json

ROOT = Path("/library")
TABLES = ("report_runs", "media_files", "duplicate_groups", "operations")
CREATED = datetime(2024, 3, 5, 10, 0, tzinfo=UTC)


def _media(name: str, *, metadata: MediaMetadata | None = None) -> MediaFile:
    """Build an image ``MediaFile`` under the test root.

    Args:
        name: Filename (with extension) of the media file.
        metadata: Optional extracted metadata.

    Returns:
        A populated media file.
    """
    return MediaFile(
        path=ROOT / name,
        media_type=MediaType.IMAGE,
        creation_date=CREATED,
        extension=Path(name).suffix,
        size_bytes=1024,
        metadata=metadata,
    )


def _row_counts(
    database: ReportDatabase,
    *,
    run_id: int | None = None,
) -> dict[str, int]:
    """Count rows per report table, optionally scoped to one run.

    Args:
        database: Connected report database.
        run_id: Restrict counts to this run when given.

    Returns:
        Mapping from table name to row count.
    """
    counts: dict[str, int] = {}
    for table in TABLES:
        if run_id is None:
            rows = database._query(
                f"SELECT COUNT(*) AS n FROM {table};",  # nosec B608 - fixed table names
            )
        else:
            column = "id" if table == "report_runs" else "run_id"
            rows = database._query(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?;",  # nosec B608 - fixed table names
                (run_id,),
            )
        counts[table] = rows[0]["n"]
    return counts


@pytest.fixture
def sample_export() -> RunExport:
    """Build a run with 5 files, 1 group of 2 and 2 operations.

    Returns:
        The sample run export.
    """
    files = [_media(f"IMG_{index:04d}.jpg") for index in range(1, 6)]
    group = DuplicateGroup(
        group_number=1,
        media_type=MediaType.IMAGE,
        files=[files[0].path, files[1].path],
        target_path=files[1].path,
    )
    operations = [
        OperationLog(
            operation=FileOperation.MOVE,
            source=files[0].path,
            destination=ROOT / "dupes" / "IMG_0001.jpg",
        ),
        OperationLog(
            operation=FileOperation.COPY,
            source=files[2].path,
            destination=ROOT / "sorted" / "IMG_0003.jpg",
        ),
    ]
    return RunExport(
        root_path=ROOT,
        files=files,
        groups=[group],
        operations=operations,
        notes="session-1",
        content_hashes={files[0].path: "abc123"},
        quality_scores={files[1].path: 0.75},
    )


def test_export_run_round_trip(
    report_db: ReportDatabase,
    sample_export: RunExport,
) -> None:
    """A full export links files, groups and operations to each other."""
    run_id = export_run(report_db, sample_export)

    run = report_db.get_run(run_id)
    assert_that(run).is_not_none()
    if run is None:
        pytest.fail("expected exported run to exist")
    assert_that(run.total_files).is_equal_to(5)
    assert_that(run.duplicate_count).is_equal_to(2)
    assert_that(run.status).is_equal_to(RunStatus.COMPLETED)
    assert_that(run.completed_at).is_not_none()
    assert_that(run.notes).is_equal_to("session-1")

    files = {record.path: record for record in report_db.list_media_files(run_id)}
    assert_that(files).is_length(5)
    (group,) = report_db.list_duplicate_groups(run_id)
    members = [files[str(path)] for path in sample_export.groups[0].files]
    assert_that([member.group_id for member in members]).is_equal_to(
        [group.id, group.id],
    )
    assert_that(files[str(ROOT / "IMG_0003.jpg")].group_id).is_none()
    assert_that(group.file_count).is_equal_to(2)
    assert_that(group.best_file_id).is_equal_to(files[str(ROOT / "IMG_0002.jpg")].id)
    assert_that(files[str(ROOT / "IMG_0001.jpg")].content_hash).is_equal_to("abc123")
    assert_that(files[str(ROOT / "IMG_0002.jpg")].quality_score).is_equal_to(0.75)

    operations = report_db.list_operations(run_id)
    assert_that(operations).is_length(2)
    for record in operations:
        assert_that(record.source).is_not_none()
        assert_that(record.media_file_id).is_equal_to(files[str(record.source)].id)


def test_export_run_is_idempotent_with_run_id(
    report_db: ReportDatabase,
    sample_export: RunExport,
) -> None:
    """Re-exporting under the same run id replaces rows without double-counting."""
    first_id = export_run(report_db, sample_export)
    before = _row_counts(report_db, run_id=first_id)

    second_id = export_run(report_db, sample_export, run_id=first_id)

    assert_that(second_id).is_equal_to(first_id)
    assert_that(_row_counts(report_db, run_id=first_id)).is_equal_to(before)
    assert_that(_row_counts(report_db)).is_equal_to(before)


def test_export_run_with_unknown_run_id_raises_and_writes_nothing(
    report_db: ReportDatabase,
) -> None:
    """Replacing a run that does not exist is an error, not an insert."""
    export = RunExport(root_path=ROOT, files=[_media("a.jpg")])

    with pytest.raises(ReportError) as exc_info:
        export_run(report_db, export, run_id=999)

    assert_that(exc_info.value.message).is_equal_to("run to replace does not exist")
    assert_that(exc_info.value.context.details).is_equal_to({"run_id": 999})
    assert_that(_row_counts(report_db)).is_equal_to(dict.fromkeys(TABLES, 0))


def test_export_run_rolls_back_on_failure(
    report_db: ReportDatabase,
    sample_export: RunExport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure part-way through the export leaves every table empty."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(export_module, "_insert_operations", _boom)

    with pytest.raises(RuntimeError):
        export_run(report_db, sample_export)

    assert_that(_row_counts(report_db)).is_equal_to(dict.fromkeys(TABLES, 0))


def test_export_run_populates_fts_index(
    report_db: ReportDatabase,
    sample_export: RunExport,
) -> None:
    """Exported filenames are searchable through the FTS triggers."""
    run_id = export_run(report_db, sample_export)

    results = report_db.search_media_files("IMG_0001")

    assert_that(results).is_length(1)
    assert_that(results[0].run_id).is_equal_to(run_id)
    assert_that(results[0].filename).is_equal_to("IMG_0001.jpg")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2024, 3, 5, 10, 0, tzinfo=UTC), "2024-03-05T10:00:00Z"),
        (datetime(2024, 3, 5, 10, 0), "2024-03-05T10:00:00Z"),
        (
            datetime(2024, 3, 5, 12, 0, tzinfo=timezone(timedelta(hours=2))),
            "2024-03-05T10:00:00Z",
        ),
    ],
    ids=["aware_utc", "naive", "aware_offset"],
)
def test_format_creation_date(value: datetime, expected: str) -> None:
    """Creation dates are rendered as UTC ``YYYY-MM-DDTHH:MM:SSZ``."""
    assert_that(_format_creation_date(value)).is_equal_to(expected)


def test_creation_date_is_stored_in_contract_format(
    report_db: ReportDatabase,
) -> None:
    """Aware and naive datetimes store the same creation_date string."""
    aware = _media("aware.jpg")
    naive = _media("naive.jpg").model_copy(
        update={"creation_date": datetime(2024, 3, 5, 10, 0)},
    )

    run_id = export_run(report_db, RunExport(root_path=ROOT, files=[aware, naive]))

    stored = {r.filename: r.creation_date for r in report_db.list_media_files(run_id)}
    assert_that(stored).is_equal_to(
        {"aware.jpg": "2024-03-05T10:00:00Z", "naive.jpg": "2024-03-05T10:00:00Z"},
    )


def test_metadata_json_round_trip(report_db: ReportDatabase) -> None:
    """Metadata is stored as JSON without None fields and reads back equal."""
    metadata = MediaMetadata(width=640, height=480, image_format="JPEG")
    assert_that(_metadata_json(None)).is_none()

    run_id = export_run(
        report_db,
        RunExport(root_path=ROOT, files=[_media("meta.jpg", metadata=metadata)]),
    )

    (record,) = report_db.list_media_files(run_id)
    if record.metadata is None:
        pytest.fail("expected metadata to be stored")
    assert_that(json.loads(record.metadata)).is_equal_to(
        {"width": 640, "height": 480, "image_format": "JPEG"},
    )
    assert_that(MediaMetadata.model_validate_json(record.metadata)).is_equal_to(
        metadata,
    )


def test_duplicate_path_raises_and_writes_nothing(report_db: ReportDatabase) -> None:
    """Repeated file paths are rejected before anything is written."""
    export = RunExport(root_path=ROOT, files=[_media("a.jpg"), _media("a.jpg")])

    with pytest.raises(ReportError) as exc_info:
        export_run(report_db, export)

    assert_that(exc_info.value.message).is_equal_to("duplicate path in export")
    assert_that(exc_info.value.context.operation).is_equal_to("export_run")
    assert_that(_row_counts(report_db)).is_equal_to(dict.fromkeys(TABLES, 0))


def test_unknown_group_member_raises_and_writes_nothing(
    report_db: ReportDatabase,
) -> None:
    """Groups referencing paths outside ``files`` are rejected before writing."""
    group = DuplicateGroup(
        group_number=1,
        media_type=MediaType.IMAGE,
        files=[ROOT / "a.jpg", ROOT / "missing.jpg"],
    )
    export = RunExport(root_path=ROOT, files=[_media("a.jpg")], groups=[group])

    with pytest.raises(ReportError) as exc_info:
        export_run(report_db, export)

    assert_that(exc_info.value.context.operation).is_equal_to("export_run")
    assert_that(_row_counts(report_db)).is_equal_to(dict.fromkeys(TABLES, 0))


def test_member_in_two_groups_raises_and_writes_nothing(
    report_db: ReportDatabase,
) -> None:
    """A path that belongs to more than one group is rejected before writing."""
    files = [_media("a.jpg"), _media("b.jpg"), _media("c.jpg")]
    groups = [
        DuplicateGroup(
            group_number=1,
            media_type=MediaType.IMAGE,
            files=[files[0].path, files[1].path],
        ),
        DuplicateGroup(
            group_number=2,
            media_type=MediaType.IMAGE,
            files=[files[1].path, files[2].path],
        ),
    ]

    with pytest.raises(ReportError) as exc_info:
        export_run(report_db, RunExport(root_path=ROOT, files=files, groups=groups))

    assert_that(exc_info.value.message).is_equal_to(
        "duplicate group member appears in more than one group",
    )
    assert_that(exc_info.value.context.details).is_equal_to({"group_number": 2})
    assert_that(_row_counts(report_db)).is_equal_to(dict.fromkeys(TABLES, 0))


def test_repeated_member_within_group_raises(report_db: ReportDatabase) -> None:
    """A group listing the same path twice is rejected before writing."""
    files = [_media("a.jpg"), _media("b.jpg")]
    group = DuplicateGroup(
        group_number=1,
        media_type=MediaType.IMAGE,
        files=[files[0].path, files[0].path],
    )

    with pytest.raises(ReportError):
        export_run(report_db, RunExport(root_path=ROOT, files=files, groups=[group]))

    assert_that(_row_counts(report_db)).is_equal_to(dict.fromkeys(TABLES, 0))


def test_export_run_target_outside_group_has_no_best_file(
    report_db: ReportDatabase,
) -> None:
    """A target path that is not a member never becomes ``best_file_id``."""
    files = [_media("a.jpg"), _media("b.jpg"), _media("c.jpg")]
    group = DuplicateGroup(
        group_number=1,
        media_type=MediaType.IMAGE,
        files=[files[0].path, files[1].path],
        target_path=files[2].path,
    )

    run_id = export_run(
        report_db,
        RunExport(root_path=ROOT, files=files, groups=[group]),
    )

    (stored_group,) = report_db.list_duplicate_groups(run_id)
    assert_that(stored_group.best_file_id).is_none()
    assert_that(stored_group.target_path).is_equal_to(str(files[2].path))


def test_export_run_running_status_has_no_completed_at(
    report_db: ReportDatabase,
) -> None:
    """A nonterminal status leaves ``completed_at`` unset."""
    run_id = export_run(
        report_db,
        RunExport(root_path=ROOT, files=[_media("a.jpg")], status=RunStatus.RUNNING),
    )

    run = report_db.get_run(run_id)
    if run is None:
        pytest.fail("expected exported run to exist")
    assert_that(run.status).is_equal_to(RunStatus.RUNNING)
    assert_that(run.completed_at).is_none()


def test_export_run_group_without_target_has_no_best_file(
    report_db: ReportDatabase,
) -> None:
    """A group without a target path stores a NULL ``best_file_id``."""
    files = [_media("a.jpg"), _media("b.jpg")]
    group = DuplicateGroup(
        group_number=1,
        media_type=MediaType.IMAGE,
        files=[files[0].path, files[1].path],
    )
    operations = [OperationLog(operation=FileOperation.MKDIR, destination=ROOT / "x")]

    run_id = export_run(
        report_db,
        RunExport(root_path=ROOT, files=files, groups=[group], operations=operations),
    )

    (stored_group,) = report_db.list_duplicate_groups(run_id)
    assert_that(stored_group.best_file_id).is_none()
    (operation,) = report_db.list_operations(run_id)
    assert_that(operation.media_file_id).is_none()
    assert_that(operation.source).is_none()


def test_export_run_chunks_large_file_sets(report_db: ReportDatabase) -> None:
    """More files than one executemany chunk are all persisted."""
    files = [_media(f"IMG_{index:05d}.jpg") for index in range(1, 1202)]

    run_id = export_run(report_db, RunExport(root_path=ROOT, files=files))

    assert_that(report_db.list_media_files(run_id)).is_length(1201)
