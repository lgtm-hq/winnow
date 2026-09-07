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

ROOT = Path("/library")
DATE_2023 = datetime(2023, 1, 1, 0, 0, tzinfo=UTC)
DATE_2024 = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
DATE_2025 = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)

# (name, media type, size, creation date, quality score); sizes and quality
# scores are distinct and ordered differently from the names for sort tests.
LIBRARY = (
    ("IMG_0001.jpg", MediaType.IMAGE, 300, DATE_2023, 0.9),
    ("IMG_0002.jpg", MediaType.IMAGE, 600, DATE_2024, 0.4),
    ("VID_0001.mp4", MediaType.VIDEO, 5000, DATE_2024, 0.7),
    ("VID_0002.mp4", MediaType.VIDEO, 4000, DATE_2025, 0.1),
    ("AUD_0001.mp3", MediaType.AUDIO, 100, DATE_2024, 0.5),
    ("AUD_0002.mp3", MediaType.AUDIO, 200, DATE_2025, 0.3),
)
LIBRARY_SIZE = len(LIBRARY)

# Expected ascending order per sort column; equal creation dates fall back to
# insertion (``id``) order.
SORTED_NAMES: dict[MediaFileSort, list[str]] = {
    MediaFileSort.PATH: [
        "AUD_0001.mp3",
        "AUD_0002.mp3",
        "IMG_0001.jpg",
        "IMG_0002.jpg",
        "VID_0001.mp4",
        "VID_0002.mp4",
    ],
    MediaFileSort.FILENAME: [
        "AUD_0001.mp3",
        "AUD_0002.mp3",
        "IMG_0001.jpg",
        "IMG_0002.jpg",
        "VID_0001.mp4",
        "VID_0002.mp4",
    ],
    MediaFileSort.SIZE_BYTES: [
        "AUD_0001.mp3",
        "AUD_0002.mp3",
        "IMG_0001.jpg",
        "IMG_0002.jpg",
        "VID_0002.mp4",
        "VID_0001.mp4",
    ],
    MediaFileSort.CREATION_DATE: [
        "IMG_0001.jpg",
        "IMG_0002.jpg",
        "VID_0001.mp4",
        "AUD_0001.mp3",
        "VID_0002.mp4",
        "AUD_0002.mp3",
    ],
    MediaFileSort.QUALITY_SCORE: [
        "VID_0002.mp4",
        "AUD_0002.mp3",
        "IMG_0002.jpg",
        "AUD_0001.mp3",
        "VID_0001.mp4",
        "IMG_0001.jpg",
    ],
}


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
        for name, media_type, size, created, _quality in LIBRARY
    ]
    group = DuplicateGroup(
        group_number=1,
        media_type=MediaType.IMAGE,
        files=[files[0].path, files[1].path],
        target_path=files[1].path,
    )
    quality_scores = {ROOT / name: quality for name, *_rest, quality in LIBRARY}
    return export_run(
        report_db,
        RunExport(
            root_path=ROOT,
            files=files,
            groups=[group],
            quality_scores=quality_scores,
        ),
    )


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (MediaFileFilter(), SORTED_NAMES[MediaFileSort.PATH]),
        (MediaFileFilter(media_type="video"), ["VID_0001.mp4", "VID_0002.mp4"]),
        (
            MediaFileFilter(duplicate_status=DuplicateStatus.GROUPED),
            ["IMG_0001.jpg", "IMG_0002.jpg"],
        ),
        (
            MediaFileFilter(duplicate_status=DuplicateStatus.UNGROUPED),
            ["AUD_0001.mp3", "AUD_0002.mp3", "VID_0001.mp4", "VID_0002.mp4"],
        ),
        (MediaFileFilter(search="IMG_0001"), ["IMG_0001.jpg"]),
    ],
    ids=["unfiltered", "media_type", "grouped", "ungrouped", "search"],
)
def test_list_media_files_page_filters(
    report_db: ReportDatabase,
    seeded_library: int,
    filters: MediaFileFilter,
    expected: list[str],
) -> None:
    """Each filter narrows the page to exactly the documented files."""
    result = list_media_files_page(
        report_db,
        filters=filters,
        sort=MediaFileSort.PATH,
        direction=SortDirection.ASC,
        page=PageRequest(),
    )

    assert_that([item.filename for item in result.items]).is_equal_to(expected)
    assert_that(result.total).is_equal_to(len(expected))


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
    ['" OR 1=1 --', "a AND b", '"unterminated'],
    ids=["injection", "operators", "unterminated_quote"],
)
def test_hostile_search_returns_normally(
    report_db: ReportDatabase,
    seeded_library: int,
    search: str,
) -> None:
    """Hostile search input never raises and never matches the whole library."""
    result = list_media_files_page(
        report_db,
        filters=MediaFileFilter(search=search),
        sort=MediaFileSort.PATH,
        direction=SortDirection.ASC,
        page=PageRequest(),
    )

    assert_that(result.items).is_length(min(result.total, result.per_page))
    assert_that(result.total).is_less_than(LIBRARY_SIZE)


def test_all_filters_combine_to_one_file(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """Run, type, group status, date window and search all apply at once."""
    names = _all_files(
        report_db,
        filters=MediaFileFilter(
            run_id=seeded_library,
            media_type="image",
            duplicate_status=DuplicateStatus.GROUPED,
            created_from="2024-01-01T00:00:00Z",
            created_to="2025-01-01T00:00:00Z",
            search="IMG",
        ),
    )

    assert_that(names).is_equal_to(["IMG_0002.jpg"])


@pytest.mark.parametrize(
    "filters",
    [
        MediaFileFilter(search=""),
        MediaFileFilter(search="   "),
        MediaFileFilter(media_type=""),
        MediaFileFilter(media_type=" "),
        MediaFileFilter(created_from=""),
        MediaFileFilter(created_from="  "),
        MediaFileFilter(created_to=""),
        MediaFileFilter(created_to="\t"),
    ],
    ids=[
        "search_empty",
        "search_blank",
        "media_type_empty",
        "media_type_blank",
        "created_from_empty",
        "created_from_blank",
        "created_to_empty",
        "created_to_blank",
    ],
)
def test_blank_text_filter_is_no_filter(
    report_db: ReportDatabase,
    seeded_library: int,
    filters: MediaFileFilter,
) -> None:
    """An empty or whitespace-only text filter lists every file."""
    names = _all_files(report_db, filters=filters)

    assert_that(names).is_equal_to(SORTED_NAMES[MediaFileSort.PATH])


def test_sort_by_size_descending(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """``SIZE_BYTES`` / ``DESC`` orders the page by size, largest first."""
    names = _all_files(
        report_db,
        sort=MediaFileSort.SIZE_BYTES,
        direction=SortDirection.DESC,
    )

    assert_that(names).is_equal_to(
        list(reversed(SORTED_NAMES[MediaFileSort.SIZE_BYTES])),
    )


@pytest.mark.parametrize(
    "sort",
    list(MediaFileSort),
    ids=[sort.name for sort in MediaFileSort],
)
def test_every_sort_column_orders_ascending(
    report_db: ReportDatabase,
    seeded_library: int,
    sort: MediaFileSort,
) -> None:
    """Every whitelisted sort column yields the expected file sequence."""
    names = _all_files(report_db, sort=sort, direction=SortDirection.ASC)

    assert_that(names).is_equal_to(SORTED_NAMES[sort])


@pytest.fixture
def undated_file(report_db: ReportDatabase, seeded_library: int) -> str:
    """Add a file with neither ``creation_date`` nor ``quality_score``.

    Args:
        report_db: Connected report database fixture.
        seeded_library: Identifier of the seeded run.

    Returns:
        The filename of the added row.
    """
    name = "UNK_0001.jpg"
    report_db.add_media_file(
        run_id=seeded_library,
        path=ROOT / name,
        media_type=MediaType.IMAGE,
        size_bytes=1,
    )
    return name


@pytest.mark.parametrize(
    "filters",
    [
        MediaFileFilter(created_from="2000-01-01T00:00:00Z"),
        MediaFileFilter(created_to="2100-01-01T00:00:00Z"),
    ],
    ids=["created_from", "created_to"],
)
def test_date_window_excludes_undated_file(
    report_db: ReportDatabase,
    undated_file: str,
    filters: MediaFileFilter,
) -> None:
    """A NULL ``creation_date`` never satisfies a date bound."""
    names = _all_files(report_db, filters=filters)

    assert_that(names).does_not_contain(undated_file)
    assert_that(names).is_equal_to(SORTED_NAMES[MediaFileSort.PATH])


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (
            SortDirection.ASC,
            ["UNK_0001.jpg", *SORTED_NAMES[MediaFileSort.QUALITY_SCORE]],
        ),
        (
            SortDirection.DESC,
            [*reversed(SORTED_NAMES[MediaFileSort.QUALITY_SCORE]), "UNK_0001.jpg"],
        ),
    ],
    ids=["asc_null_first", "desc_null_last"],
)
def test_quality_sort_places_null_per_sqlite(
    report_db: ReportDatabase,
    undated_file: str,
    direction: SortDirection,
    expected: list[str],
) -> None:
    """A NULL ``quality_score`` sorts first ascending and last descending."""
    names = _all_files(
        report_db,
        sort=MediaFileSort.QUALITY_SCORE,
        direction=direction,
    )

    assert_that(names).is_equal_to(expected)


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


def test_list_duplicate_groups_page_paginates_across_runs(
    report_db: ReportDatabase,
    seeded_library: int,
) -> None:
    """Page 2 of size 2 holds the third group; the total spans both runs."""
    other_root = ROOT / "other"
    files = [
        MediaFile(
            path=other_root / f"IMG_{index:04d}.jpg",
            media_type=MediaType.IMAGE,
            creation_date=DATE_2024,
            extension=".jpg",
            size_bytes=index,
        )
        for index in range(1, 5)
    ]
    groups = [
        DuplicateGroup(
            group_number=number,
            media_type=MediaType.IMAGE,
            files=[files[2 * (number - 1)].path, files[2 * (number - 1) + 1].path],
        )
        for number in (1, 2)
    ]
    other = export_run(
        report_db,
        RunExport(root_path=other_root, files=files, groups=groups),
    )

    result = list_duplicate_groups_page(
        report_db,
        run_id=None,
        page=PageRequest(page=2, per_page=2),
    )

    assert_that(result.total).is_equal_to(3)
    assert_that(result.page).is_equal_to(2)
    assert_that(result.items).is_length(1)
    entry = result.items[0]
    assert_that(entry.group.run_id).is_equal_to(other)
    assert_that(entry.group.group_number).is_equal_to(2)
    assert_that([member.filename for member in entry.members]).is_equal_to(
        ["IMG_0003.jpg", "IMG_0004.jpg"],
    )


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
