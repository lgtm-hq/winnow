"""Tests for the SQLite report database persistence layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.exceptions import ReportError
from winnow.fs.operations import FileOperation, OperationStatus
from winnow.models.media import MediaType
from winnow.report import ReportDatabase, RunStatus
from winnow.report.schema import SCHEMA_VERSION
from winnow.storage import Migration


def test_connect_initializes_schema_version(report_db: ReportDatabase) -> None:
    """Connecting to a fresh database provisions the current schema version."""
    assert_that(report_db.schema_version()).is_equal_to(SCHEMA_VERSION)


def test_connect_is_idempotent(report_db: ReportDatabase) -> None:
    """Calling connect twice keeps the same connection and existing data."""
    run_id = report_db.create_run(root_path="/library")

    same = report_db.connect()

    assert_that(same).is_same_as(report_db)
    assert_that(report_db.get_run(run_id)).is_not_none()
    assert_that(report_db.schema_version()).is_equal_to(SCHEMA_VERSION)


def test_context_manager_opens_and_closes() -> None:
    """The context manager connects on entry and closes on exit."""
    database = ReportDatabase()

    with database as connected:
        assert_that(connected.is_connected).is_true()

    assert_that(database.is_connected).is_false()


def test_file_backed_database_persists_across_connections(
    tmp_path: Path,
) -> None:
    """Data written to a file database survives reconnecting."""
    db_path = tmp_path / "report.db"

    first = ReportDatabase(db_path).connect()
    run_id = first.create_run(root_path="/library")
    first.close()

    second = ReportDatabase(db_path).connect()
    try:
        assert_that(second.path).is_equal_to(db_path)
        assert_that(second.get_run(run_id)).is_not_none()
        assert_that(second.schema_version()).is_equal_to(SCHEMA_VERSION)
    finally:
        second.close()


def test_operations_before_connect_raise(tmp_path: Path) -> None:
    """Using the database before connecting raises a ReportError."""
    database = ReportDatabase(tmp_path / "report.db")

    with pytest.raises(ReportError) as exc_info:
        database.create_run(root_path="/library")

    assert_that(exc_info.value.context.operation).is_equal_to(
        "require_connection",
    )


def test_unsupported_schema_version_raises(tmp_path: Path) -> None:
    """Opening a database with a newer schema version raises ReportError."""
    db_path = tmp_path / "report.db"
    ReportDatabase(db_path).connect().close()

    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO schema_version (version) VALUES (?);",
        (SCHEMA_VERSION + 1,),
    )
    connection.commit()
    connection.close()

    database = ReportDatabase(db_path)
    with pytest.raises(ReportError) as exc_info:
        database.connect()

    assert_that(exc_info.value.context.details).contains_key("found")
    assert_that(database.is_connected).is_false()


def test_existing_v2_database_upgrades_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v2 database migrates to a newer build and keeps its rows."""
    db_path = tmp_path / "report.db"
    seeded = ReportDatabase(db_path).connect()
    run_id = seeded.create_run(root_path="/library", notes="before upgrade")
    seeded.close()

    monkeypatch.setattr(
        "winnow.report._connection.MIGRATIONS",
        (
            Migration(
                version=3,
                statements=("ALTER TABLE report_runs ADD COLUMN session_id TEXT;",),
            ),
        ),
    )
    monkeypatch.setattr("winnow.report._connection.SCHEMA_VERSION", 3)

    upgraded = ReportDatabase(db_path).connect()
    try:
        assert_that(upgraded.schema_version()).is_equal_to(3)
        run = upgraded.get_run(run_id)
        assert_that(run).is_not_none()
        if run is None:
            pytest.fail("expected run to survive the upgrade")
        assert_that(run.notes).is_equal_to("before upgrade")
    finally:
        upgraded.close()

    connection = sqlite3.connect(db_path)
    try:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(report_runs);")
        ]
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_version ORDER BY version;",
            )
        ]
    finally:
        connection.close()
    assert_that(columns).contains("session_id")
    assert_that(versions).is_equal_to([2, 3])


def test_schema_version_rejects_duplicate_rows(tmp_path: Path) -> None:
    """The schema_version primary key rejects a re-inserted version."""
    db_path = tmp_path / "report.db"
    ReportDatabase(db_path).connect().close()

    connection = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO schema_version (version) VALUES (?);",
                (SCHEMA_VERSION,),
            )
    finally:
        connection.close()


def test_create_and_get_run(report_db: ReportDatabase) -> None:
    """A created run is retrievable with default status and counters."""
    run_id = report_db.create_run(root_path="/library/photos", notes="nightly")

    run = report_db.get_run(run_id)

    assert_that(run).is_not_none()
    if run is None:
        pytest.fail("expected run to exist")
    assert_that(run.root_path).is_equal_to("/library/photos")
    assert_that(run.status).is_equal_to(RunStatus.RUNNING.value)
    assert_that(run.total_files).is_equal_to(0)
    assert_that(run.duplicate_count).is_equal_to(0)
    assert_that(run.notes).is_equal_to("nightly")
    assert_that(run.completed_at).is_none()


def test_get_missing_run_returns_none(report_db: ReportDatabase) -> None:
    """Fetching a nonexistent run returns None."""
    assert_that(report_db.get_run(999)).is_none()


def test_list_runs_orders_by_start(report_db: ReportDatabase) -> None:
    """Runs are listed in ascending start order."""
    first = report_db.create_run(root_path="/a")
    second = report_db.create_run(root_path="/b")

    listed = [run.id for run in report_db.list_runs()]

    assert_that(listed).is_equal_to([first, second])


def test_update_run_status(report_db: ReportDatabase, seeded_run: int) -> None:
    """Updating to a terminal status also records the completion time."""
    updated = report_db.update_run_status(
        seeded_run,
        status=RunStatus.FAILED,
    )

    run = report_db.get_run(seeded_run)
    assert_that(updated).is_true()
    assert_that(run).is_not_none()
    if run is None:
        pytest.fail("expected run to exist")
    assert_that(run.status).is_equal_to(RunStatus.FAILED.value)
    assert_that(run.completed_at).is_not_none()


def test_update_run_status_to_running_clears_completion(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Moving a run back to a nonterminal status clears completed_at."""
    report_db.complete_run(seeded_run)

    report_db.update_run_status(seeded_run, status=RunStatus.RUNNING)

    run = report_db.get_run(seeded_run)
    assert_that(run).is_not_none()
    if run is None:
        pytest.fail("expected run to exist")
    assert_that(run.status).is_equal_to(RunStatus.RUNNING.value)
    assert_that(run.completed_at).is_none()


def test_update_run_status_rejects_unknown_status(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """An unknown status string is rejected before touching the database."""
    with pytest.raises(ReportError) as exc_info:
        report_db.update_run_status(seeded_run, status="paused")

    assert_that(exc_info.value.context.details).contains_entry(
        {"status": "paused"},
    )


def test_complete_run_rejects_nonterminal_status(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Completing a run with a nonterminal status raises a ReportError."""
    with pytest.raises(ReportError) as exc_info:
        report_db.complete_run(seeded_run, status=RunStatus.RUNNING)

    assert_that(exc_info.value.context.operation).is_equal_to("complete_run")

    run = report_db.get_run(seeded_run)
    assert_that(run).is_not_none()
    if run is None:
        pytest.fail("expected run to exist")
    assert_that(run.completed_at).is_none()


def test_create_run_rejects_invalid_status_at_storage(
    report_db: ReportDatabase,
) -> None:
    """The CHECK constraint rejects status strings outside the enum."""
    with pytest.raises(ReportError) as exc_info:
        report_db.create_run(root_path="/library", status="bogus")

    assert_that(exc_info.value.context.operation).is_equal_to("write")


def test_update_missing_run_returns_false(report_db: ReportDatabase) -> None:
    """Updating a nonexistent run reports no rows changed."""
    assert_that(
        report_db.update_run_status(404, status=RunStatus.FAILED),
    ).is_false()


def test_complete_run_sets_counts_and_timestamp(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Completing a run records status, counters, and a completion time."""
    completed = report_db.complete_run(
        seeded_run,
        total_files=12,
        duplicate_count=3,
    )

    run = report_db.get_run(seeded_run)
    assert_that(completed).is_true()
    assert_that(run).is_not_none()
    if run is None:
        pytest.fail("expected run to exist")
    assert_that(run.status).is_equal_to(RunStatus.COMPLETED.value)
    assert_that(run.total_files).is_equal_to(12)
    assert_that(run.duplicate_count).is_equal_to(3)
    assert_that(run.completed_at).is_not_none()


def test_complete_run_without_counts_keeps_defaults(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Completing without counters leaves existing counters untouched."""
    report_db.complete_run(seeded_run)

    run = report_db.get_run(seeded_run)
    assert_that(run).is_not_none()
    if run is None:
        pytest.fail("expected run to exist")
    assert_that(run.total_files).is_equal_to(0)
    assert_that(run.completed_at).is_not_none()


def test_add_and_get_media_file(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """A media file is stored with a filename derived from its path."""
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/sunset.jpg",
        media_type=MediaType.IMAGE,
        size_bytes=2048,
        content_hash="abc123",
    )

    record = report_db.get_media_file(file_id)

    assert_that(record).is_not_none()
    if record is None:
        pytest.fail("expected media file to exist")
    assert_that(record.filename).is_equal_to("sunset.jpg")
    assert_that(record.media_type).is_equal_to(MediaType.IMAGE.value)
    assert_that(record.size_bytes).is_equal_to(2048)
    assert_that(record.content_hash).is_equal_to("abc123")
    assert_that(record.group_id).is_none()


def test_add_media_file_with_explicit_filename(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """An explicit filename overrides the path-derived name."""
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/IMG_0001.CR2",
        media_type="image",
        filename="renamed.cr2",
    )

    record = report_db.get_media_file(file_id)

    assert_that(record).is_not_none()
    if record is None:
        pytest.fail("expected media file to exist")
    assert_that(record.filename).is_equal_to("renamed.cr2")


def test_list_media_files_orders_by_path(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Media files are listed in ascending path order."""
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/b.jpg",
        media_type="image",
    )
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/a.jpg",
        media_type="image",
    )

    paths = [record.path for record in report_db.list_media_files(seeded_run)]

    assert_that(paths).is_equal_to(
        ["/library/photos/a.jpg", "/library/photos/b.jpg"],
    )


def test_add_media_file_unknown_run_raises(
    report_db: ReportDatabase,
) -> None:
    """Inserting a media file for a missing run violates the foreign key."""
    with pytest.raises(ReportError) as exc_info:
        report_db.add_media_file(
            run_id=555,
            path="/library/photos/orphan.jpg",
            media_type="image",
        )

    assert_that(exc_info.value.context.operation).is_equal_to("write")


def test_add_and_list_duplicate_groups(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Duplicate groups are listed in group-number order for a run."""
    report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=2,
        media_type=MediaType.VIDEO,
    )
    report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type=MediaType.IMAGE,
        file_count=3,
        target_path="/library/photos/keep.jpg",
    )

    groups = report_db.list_duplicate_groups(seeded_run)

    assert_that([group.group_number for group in groups]).is_equal_to([1, 2])
    assert_that(groups[0].file_count).is_equal_to(3)
    assert_that(groups[0].target_path).is_equal_to("/library/photos/keep.jpg")


def test_get_duplicate_group(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """A duplicate group is retrievable by identifier."""
    group_id = report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type="image",
    )

    group = report_db.get_duplicate_group(group_id)

    assert_that(group).is_not_none()
    if group is None:
        pytest.fail("expected group to exist")
    assert_that(group.media_type).is_equal_to(MediaType.IMAGE.value)


def test_get_missing_duplicate_group_returns_none(
    report_db: ReportDatabase,
) -> None:
    """Fetching a nonexistent duplicate group returns None."""
    assert_that(report_db.get_duplicate_group(321)).is_none()


def test_duplicate_group_number_unique_per_run(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """The unique index rejects duplicate group numbers within a run."""
    report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type="image",
    )

    with pytest.raises(ReportError):
        report_db.add_duplicate_group(
            run_id=seeded_run,
            group_number=1,
            media_type="image",
        )


def test_assign_and_clear_media_file_group(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """A media file can be assigned to and cleared from a duplicate group."""
    group_id = report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type="image",
    )
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/dup.jpg",
        media_type="image",
    )

    assigned = report_db.assign_media_file_group(file_id, group_id=group_id)
    after_assign = report_db.get_media_file(file_id)
    report_db.assign_media_file_group(file_id, group_id=None)
    after_clear = report_db.get_media_file(file_id)

    assert_that(assigned).is_true()
    assert_that(after_assign).is_not_none()
    assert_that(after_clear).is_not_none()
    if after_assign is None or after_clear is None:
        pytest.fail("expected media file to exist")
    assert_that(after_assign.group_id).is_equal_to(group_id)
    assert_that(after_clear.group_id).is_none()


def test_deleting_group_nulls_media_file_group(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Deleting a duplicate group clears the group id on its media files."""
    group_id = report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type="image",
    )
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/dup.jpg",
        media_type="image",
        group_id=group_id,
    )

    deleted = report_db.delete_duplicate_group(group_id)
    record = report_db.get_media_file(file_id)

    assert_that(deleted).is_true()
    assert_that(report_db.get_duplicate_group(group_id)).is_none()
    assert_that(record).is_not_none()
    if record is None:
        pytest.fail("expected media file to exist")
    assert_that(record.group_id).is_none()


def test_delete_missing_duplicate_group_returns_false(
    report_db: ReportDatabase,
) -> None:
    """Deleting a nonexistent duplicate group reports no rows removed."""
    assert_that(report_db.delete_duplicate_group(404)).is_false()


def test_set_group_best_file(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """A group records the media file selected as its best copy."""
    group_id = report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type="image",
    )
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/best.jpg",
        media_type="image",
        group_id=group_id,
    )

    updated = report_db.set_group_best_file(group_id, file_id=file_id)

    group = report_db.get_duplicate_group(group_id)
    assert_that(updated).is_true()
    assert_that(group).is_not_none()
    if group is None:
        pytest.fail("expected group to exist")
    assert_that(group.best_file_id).is_equal_to(file_id)


def test_set_group_best_file_rejects_other_run_file(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """The composite foreign key rejects a best file from another run."""
    group_id = report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type="image",
    )
    other_run = report_db.create_run(root_path="/other")
    other_file = report_db.add_media_file(
        run_id=other_run,
        path="/other/best.jpg",
        media_type="image",
    )

    with pytest.raises(ReportError):
        report_db.set_group_best_file(group_id, file_id=other_file)


def test_assign_media_file_group_rejects_other_run_group(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """The composite foreign key rejects a group from another run."""
    other_run = report_db.create_run(root_path="/other")
    other_group = report_db.add_duplicate_group(
        run_id=other_run,
        group_number=1,
        media_type="image",
    )
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/dup.jpg",
        media_type="image",
    )

    with pytest.raises(ReportError):
        report_db.assign_media_file_group(file_id, group_id=other_group)


def test_search_media_files_by_filename(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Full-text search matches on filename fragments with punctuation."""
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/vacation_beach.jpg",
        media_type="image",
    )
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/receipt.pdf",
        media_type="image",
    )

    by_word = report_db.search_media_files("vacation")
    by_extension = report_db.search_media_files("beach.jpg")

    assert_that([record.filename for record in by_word]).is_equal_to(
        ["vacation_beach.jpg"],
    )
    assert_that([record.filename for record in by_extension]).is_equal_to(
        ["vacation_beach.jpg"],
    )


def test_media_file_persists_quality_and_metadata(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Quality score and metadata text round-trip through storage."""
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/sharp.jpg",
        media_type="image",
        quality_score=0.92,
        metadata="Canon EOS R5 f/1.8 Paris",
    )

    record = report_db.get_media_file(file_id)

    assert_that(record).is_not_none()
    if record is None:
        pytest.fail("expected media file to exist")
    assert_that(record.quality_score).is_close_to(0.92, 1e-9)
    assert_that(record.metadata).is_equal_to("Canon EOS R5 f/1.8 Paris")


def test_search_media_files_by_metadata(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Full-text search matches metadata text, not just paths."""
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/IMG_0001.jpg",
        media_type="image",
        metadata="Canon EOS R5 Paris",
    )
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/IMG_0002.jpg",
        media_type="image",
        metadata="Sony A7 Tokyo",
    )

    matches = report_db.search_media_files("paris")

    assert_that([record.filename for record in matches]).is_equal_to(
        ["IMG_0001.jpg"],
    )


def test_search_media_files_after_metadata_update(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """The FTS update trigger reindexes rows when metadata changes."""
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/IMG_0003.jpg",
        media_type="image",
        metadata="untagged",
    )

    updated = report_db.update_media_file_metadata(
        file_id,
        metadata="sunset lisbon",
    )

    assert_that(updated).is_true()

    assert_that(report_db.search_media_files("untagged")).is_empty()
    assert_that(
        [record.id for record in report_db.search_media_files("lisbon")],
    ).is_equal_to([file_id])


def test_search_media_files_scoped_to_run(
    report_db: ReportDatabase,
) -> None:
    """Search results can be restricted to a single run."""
    run_a = report_db.create_run(root_path="/a")
    run_b = report_db.create_run(root_path="/b")
    report_db.add_media_file(
        run_id=run_a,
        path="/a/holiday.jpg",
        media_type="image",
    )
    report_db.add_media_file(
        run_id=run_b,
        path="/b/holiday.jpg",
        media_type="image",
    )

    scoped = report_db.search_media_files("holiday", run_id=run_a)

    assert_that([record.run_id for record in scoped]).is_equal_to([run_a])


def test_search_empty_query_returns_no_results(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """A blank query short-circuits to an empty result set."""
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/a.jpg",
        media_type="image",
    )

    assert_that(report_db.search_media_files("   ")).is_empty()


def test_search_raw_query_uses_fts_operators(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """A raw query is passed through as an FTS5 match expression."""
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/beach.jpg",
        media_type="image",
    )
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/forest.jpg",
        media_type="image",
    )

    matches = report_db.search_media_files("beach OR forest", raw=True)

    assert_that([record.filename for record in matches]).contains_only(
        "beach.jpg",
        "forest.jpg",
    )


def test_search_invalid_raw_query_raises(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """An invalid raw FTS expression surfaces as a ReportError."""
    report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/a.jpg",
        media_type="image",
    )

    with pytest.raises(ReportError):
        report_db.search_media_files("a.jpg", raw=True)


def test_add_and_list_operations(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Operations are recorded and listed in insertion order."""
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/a.jpg",
        media_type="image",
    )
    report_db.add_operation(
        run_id=seeded_run,
        operation=FileOperation.MOVE,
        status=OperationStatus.APPLIED,
        media_file_id=file_id,
        source="/library/photos/a.jpg",
        destination="/library/keep/a.jpg",
    )
    report_db.add_operation(
        run_id=seeded_run,
        operation=FileOperation.DELETE,
        status=OperationStatus.ROLLED_BACK,
    )

    operations = report_db.list_operations(seeded_run)

    assert_that([op.operation for op in operations]).is_equal_to(
        [FileOperation.MOVE.value, FileOperation.DELETE.value],
    )
    assert_that(operations[0].destination).is_equal_to(
        "/library/keep/a.jpg",
    )
    assert_that(operations[1].media_file_id).is_none()


def test_add_operation_rejects_other_run_media_file(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """The composite foreign key rejects a media file from another run."""
    other_run = report_db.create_run(root_path="/other")
    other_file = report_db.add_media_file(
        run_id=other_run,
        path="/other/a.jpg",
        media_type="image",
    )

    with pytest.raises(ReportError):
        report_db.add_operation(
            run_id=seeded_run,
            operation=FileOperation.MOVE,
            status=OperationStatus.APPLIED,
            media_file_id=other_file,
        )


def test_add_operation_rejects_invalid_status(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """The CHECK constraint rejects operation statuses outside the enum."""
    with pytest.raises(ReportError):
        report_db.add_operation(
            run_id=seeded_run,
            operation=FileOperation.MOVE,
            status="pending",
        )


def test_delete_run_cascades(
    report_db: ReportDatabase,
    seeded_run: int,
) -> None:
    """Deleting a run removes its media files, groups, and operations."""
    group_id = report_db.add_duplicate_group(
        run_id=seeded_run,
        group_number=1,
        media_type="image",
    )
    file_id = report_db.add_media_file(
        run_id=seeded_run,
        path="/library/photos/a.jpg",
        media_type="image",
        group_id=group_id,
    )
    report_db.add_operation(
        run_id=seeded_run,
        operation="move",
        status="applied",
        media_file_id=file_id,
    )

    deleted = report_db.delete_run(seeded_run)

    assert_that(deleted).is_true()
    assert_that(report_db.get_run(seeded_run)).is_none()
    assert_that(report_db.list_media_files(seeded_run)).is_empty()
    assert_that(report_db.list_duplicate_groups(seeded_run)).is_empty()
    assert_that(report_db.list_operations(seeded_run)).is_empty()


def test_delete_missing_run_returns_false(
    report_db: ReportDatabase,
) -> None:
    """Deleting a nonexistent run reports no rows removed."""
    assert_that(report_db.delete_run(777)).is_false()


def test_close_is_idempotent(report_db: ReportDatabase) -> None:
    """Closing an already-closed database is a safe no-op."""
    report_db.close()
    report_db.close()

    assert_that(report_db.is_connected).is_false()
