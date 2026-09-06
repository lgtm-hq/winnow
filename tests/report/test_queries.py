"""Tests for the filtered, sorted, paginated report queries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.models.duplicates import DuplicateGroup
from winnow.models.media import MediaFile, MediaType
from winnow.report import (
    MAX_PER_PAGE,
    DuplicateStatus,
    MediaFileFilter,
    MediaFileSort,
    PageRequest,
    ReportDatabase,
    RunExport,
    SortDirection,
    export_run,
    list_duplicate_groups_page,
    list_media_files_page,
    list_runs_page,
)
from winnow.report.queries import _media_file_where

ROOT = Path("/library")
DATE_2023 = datetime(2023, 1, 1, 0, 0, tzinfo=UTC)
DATE_2024 = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
DATE_2025 = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)

# (name, media type, size, creation date); sizes are distinct for sort tests.
LIBRARY = (
    ("IMG_0001.jpg", MediaType.IMAGE, 300, DATE_2023),
    ("IMG_0002.jpg", MediaType.IMAGE, 600, DATE_2024),
    ("VID_0001.mp4", MediaType.VIDEO, 5000, DATE_2024),
    ("VID_0002.mp4", MediaType.VIDEO, 4000, DATE_2025),
    ("AUD_0001.mp3", MediaType.AUDIO, 100, DATE_2024),
    ("AUD_0002.mp3", MediaType.AUDIO, 200, DATE_2025),
)
LIBRARY_SIZE = len(LIBRARY)


def _all_files(
    db: ReportDatabase,
    *,
    filters: MediaFileFilter | None = None,
    sort: MediaFileSort = MediaFileSort.PATH,
    direction: SortDirection = SortDirection.ASC,
    page: PageRequest | None = None,
) -> list[str]:
    """List matching file names via ``list_media_files_page``.

    Args:
        db: Connected report database.
        filters: Filters to apply; unfiltered when omitted.
        sort: Primary sort column.
        direction: Sort direction.
        page: Page to fetch; the default page when omitted.

    Returns:
        The file names on the page, in listing order.
    """
    result = list_media_files_page(
        db,
        filters=filters or MediaFileFilter(),
        sort=sort,
        direction=direction,
        page=page or PageRequest(),
    )
    return [item.filename for item in result.items]


@pytest.fixture
def seeded_library(report_db: ReportDatabase) -> int:
    """Export a run with six files, two of which form one duplicate group.

    Args:
        report_db: Connected report database fixture.

    Returns:
        The identifier of the exported run.
    """
    files = [
        MediaFile(
            path=ROOT / name,
            media_type=media_type,
            creation_date=created,
            extension=Path(name).suffix,
            size_bytes=size,
        )
        for name, media_type, size, created in LIBRARY
    ]
    group = DuplicateGroup(
        group_number=1,
        media_type=MediaType.IMAGE,
        files=[files[0].path, files[1].path],
        target_path=files[1].path,
    )
    return export_run(report_db, RunExport(root_path=ROOT, files=files, groups=[group]))


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (MediaFileFilter(), LIBRARY_SIZE),
        (MediaFileFilter(media_type="video"), 2),
        (MediaFileFilter(duplicate_status=DuplicateStatus.GROUPED), 2),
        (MediaFileFilter(duplicate_status=DuplicateStatus.UNGROUPED), 4),
        (MediaFileFilter(search="IMG_0001"), 1),
    ],
    ids=["unfiltered", "media_type", "grouped", "ungrouped", "search"],
)
def test_list_media_files_page_filters(
    report_db: ReportDatabase,
    seeded_library: int,
    filters: MediaFileFilter,
    expected: int,
) -> None:
    """Each filter narrows the page and the total to the documented count."""
    result = list_media_files_page(
        report_db,
        filters=filters,
        sort=MediaFileSort.PATH,
        direction=SortDirection.ASC,
        page=PageRequest(),
    )

    assert_that(result.items).is_length(expected)
    assert_that(result.total).is_equal_to(expected)


def test_run_id_filter_scopes_to_one_run(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """Files from another run are excluded by ``run_id``."""
    other = export_run(
        report_db,
        RunExport(
            root_path=ROOT / "other",
            files=[
                MediaFile(
                    path=ROOT / "other" / "IMG_0009.jpg",
                    media_type=MediaType.IMAGE,
                    creation_date=DATE_2024,
                    extension=".jpg",
                    size_bytes=1,
                ),
            ],
        ),
    )

    names = _all_files(report_db, filters=MediaFileFilter(run_id=other))
    everything = _all_files(report_db)

    assert_that(names).is_equal_to(["IMG_0009.jpg"])
    assert_that(everything).is_length(LIBRARY_SIZE + 1)


def test_created_from_is_inclusive(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """``created_from`` keeps the 2024 and 2025 files and drops 2023."""
    names = _all_files(
        report_db,
        filters=MediaFileFilter(created_from="2024-01-01T00:00:00Z"),
    )

    assert_that(names).is_equal_to(
        [
            "AUD_0001.mp3",
            "AUD_0002.mp3",
            "IMG_0002.jpg",
            "VID_0001.mp4",
            "VID_0002.mp4",
        ],
    )


def test_created_to_is_exclusive(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """``created_to`` keeps only the 2023 file."""
    names = _all_files(
        report_db,
        filters=MediaFileFilter(created_to="2024-01-01T00:00:00Z"),
    )

    assert_that(names).is_equal_to(["IMG_0001.jpg"])


def test_created_to_boundary_excludes_exact_match(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """A file created exactly at ``created_to`` is excluded."""
    names = _all_files(
        report_db,
        filters=MediaFileFilter(
            created_from="2025-01-01T00:00:00Z",
            created_to="2025-01-01T00:00:00Z",
        ),
    )

    assert_that(names).is_empty()


def test_search_matches_filename_fragment(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """A filename fragment matches exactly the file carrying it."""
    names = _all_files(report_db, filters=MediaFileFilter(search="IMG_0001"))

    assert_that(names).is_equal_to(["IMG_0001.jpg"])


def test_search_combines_with_other_filters(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """The FTS join and the plain filters apply together."""
    names = _all_files(
        report_db,
        filters=MediaFileFilter(search="library", media_type="audio"),
        sort=MediaFileSort.SIZE_BYTES,
        direction=SortDirection.DESC,
    )

    assert_that(names).is_equal_to(["AUD_0002.mp3", "AUD_0001.mp3"])


@pytest.mark.parametrize(
    "search",
    ['" OR 1=1 --', "a AND b", '"unterminated', "   "],
    ids=["injection", "operators", "unterminated_quote", "blank"],
)
def test_hostile_search_returns_normally(
    report_db: ReportDatabase,
    seeded_library: int,
    search: str,
) -> None:
    """Hostile or blank search input never raises."""
    result = list_media_files_page(
        report_db,
        filters=MediaFileFilter(search=search),
        sort=MediaFileSort.PATH,
        direction=SortDirection.ASC,
        page=PageRequest(),
    )

    assert_that(result.total).is_greater_than_or_equal_to(0)
    assert_that(result.items).is_length(min(result.total, result.per_page))


def test_blank_search_is_no_filter(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """A whitespace-only search lists every file."""
    names = _all_files(report_db, filters=MediaFileFilter(search="   "))

    assert_that(names).is_length(LIBRARY_SIZE)


def test_sort_by_size_descending(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """``SIZE_BYTES`` / ``DESC`` orders the page by size, largest first."""
    result = list_media_files_page(
        report_db,
        filters=MediaFileFilter(),
        sort=MediaFileSort.SIZE_BYTES,
        direction=SortDirection.DESC,
        page=PageRequest(),
    )

    sizes = [item.size_bytes for item in result.items]
    assert_that(sizes).is_equal_to(sorted(sizes, reverse=True))
    assert_that(sizes).is_length(LIBRARY_SIZE)


@pytest.mark.parametrize(
    "sort",
    list(MediaFileSort),
    ids=[sort.name for sort in MediaFileSort],
)
def test_every_sort_column_is_accepted(
    report_db: ReportDatabase,
    seeded_library: int,
    sort: MediaFileSort,
) -> None:
    """Every whitelisted sort column produces a full page."""
    names = _all_files(report_db, sort=sort, direction=SortDirection.ASC)

    assert_that(names).is_length(LIBRARY_SIZE)


def test_pagination_returns_slice_and_total(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """Page 2 of size 2 holds the 3rd and 4th files by path; total is 6."""
    result = list_media_files_page(
        report_db,
        filters=MediaFileFilter(),
        sort=MediaFileSort.PATH,
        direction=SortDirection.ASC,
        page=PageRequest(page=2, per_page=2),
    )

    names = [item.filename for item in result.items]
    assert_that(names).is_equal_to(["IMG_0001.jpg", "IMG_0002.jpg"])
    assert_that(result.total).is_equal_to(LIBRARY_SIZE)
    assert_that(result.page).is_equal_to(2)
    assert_that(result.per_page).is_equal_to(2)


def test_page_past_the_end_is_empty_with_total(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """A page beyond the last row is empty but still reports the total."""
    result = list_media_files_page(
        report_db,
        filters=MediaFileFilter(),
        sort=MediaFileSort.PATH,
        direction=SortDirection.ASC,
        page=PageRequest(page=4, per_page=2),
    )

    assert_that(result.items).is_empty()
    assert_that(result.total).is_equal_to(LIBRARY_SIZE)


@pytest.mark.parametrize(
    ("page", "per_page"),
    [(1, MAX_PER_PAGE + 1), (1, 0), (0, 10), (-1, 10)],
    ids=["per_page_over_max", "per_page_zero", "page_zero", "page_negative"],
)
def test_page_request_rejects_out_of_range(page: int, per_page: int) -> None:
    """``PageRequest`` validates its bounds in ``__post_init__``."""
    with pytest.raises(ValueError):
        PageRequest(page=page, per_page=per_page)


def test_page_request_defaults_and_offset() -> None:
    """Defaults are page 1 of 50; offset skips the earlier pages."""
    assert_that(PageRequest()).is_equal_to(PageRequest(page=1, per_page=50))
    assert_that(PageRequest(page=3, per_page=20).offset).is_equal_to(40)
    assert_that(PageRequest(per_page=MAX_PER_PAGE).per_page).is_equal_to(
        MAX_PER_PAGE,
    )


def test_list_duplicate_groups_page_returns_group_with_members(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """The seeded group comes back with its two members in path order."""
    result = list_duplicate_groups_page(
        report_db,
        run_id=seeded_library,
        page=PageRequest(),
    )

    assert_that(result.total).is_equal_to(1)
    assert_that(result.items).is_length(1)
    entry = result.items[0]
    assert_that(entry.group.group_number).is_equal_to(1)
    assert_that(entry.group.file_count).is_equal_to(2)
    assert_that([member.filename for member in entry.members]).is_equal_to(
        ["IMG_0001.jpg", "IMG_0002.jpg"],
    )
    assert_that({member.group_id for member in entry.members}).is_equal_to(
        {entry.group.id},
    )


def test_list_duplicate_groups_page_filters_by_run(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """Another run's id yields no groups; ``None`` yields every run's groups."""
    scoped = list_duplicate_groups_page(
        report_db,
        run_id=seeded_library + 1,
        page=PageRequest(),
    )
    unscoped = list_duplicate_groups_page(report_db, run_id=None, page=PageRequest())

    assert_that(scoped.items).is_empty()
    assert_that(scoped.total).is_equal_to(0)
    assert_that(unscoped.total).is_equal_to(1)


def test_list_runs_page_returns_the_run(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """The exported run is listed with its counters."""
    result = list_runs_page(report_db, page=PageRequest())

    assert_that(result.total).is_equal_to(1)
    assert_that(result.items).is_length(1)
    run = result.items[0]
    assert_that(run.id).is_equal_to(seeded_library)
    assert_that(run.total_files).is_equal_to(LIBRARY_SIZE)
    assert_that(run.duplicate_count).is_equal_to(2)


def test_list_runs_page_paginates(report_db: ReportDatabase) -> None:
    """Runs are paginated oldest first with the total over all runs."""
    run_ids = [
        report_db.create_run(root_path=f"/library/{index}") for index in range(3)
    ]

    result = list_runs_page(report_db, page=PageRequest(page=2, per_page=2))

    assert_that([run.id for run in result.items]).is_equal_to([run_ids[2]])
    assert_that(result.total).is_equal_to(3)


def test_media_file_where_binds_every_value() -> None:
    """The shared WHERE clause binds one parameter per value filter."""
    where, params = _media_file_where(
        MediaFileFilter(
            run_id=7,
            media_type="image",
            duplicate_status=DuplicateStatus.GROUPED,
            created_from="2024-01-01T00:00:00Z",
            created_to="2025-01-01T00:00:00Z",
            search="IMG_0001",
        ),
    )

    assert_that(where.count("?")).is_equal_to(len(params))
    assert_that(params).is_equal_to(
        [7, "image", "2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z", '"IMG_0001"'],
    )
    assert_that(where).contains("group_id IS NOT NULL", "media_files_fts MATCH ?")


def test_media_file_where_unfiltered_is_empty() -> None:
    """No filters produce no WHERE clause and no parameters."""
    assert_that(_media_file_where(MediaFileFilter())).is_equal_to(("", []))
