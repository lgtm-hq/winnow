"""Filtered, sorted, paginated read queries over the report database.

This module is the one place that knows how to turn a filter, a sort key and
a page request into SQL. Both the CLI ``report`` commands and the HTTP API
consume it, so the two transports share a single vocabulary by construction:
:class:`MediaFileFilter` names the filters, :class:`MediaFileSort` and
:class:`SortDirection` whitelist the ``ORDER BY`` clause, and
:class:`PageRequest` bounds the page size.

Every value is bound as a SQL parameter. Free-text ``search`` is routed
through :meth:`~winnow.report._media.MediaStore._build_match_query`, so FTS5
operators in user input are quoted rather than interpreted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Generic, TypeVar

from winnow.report._media import MediaStore
from winnow.report.database import ReportDatabase
from winnow.report.records import DuplicateGroupRecord, MediaFileRecord, ReportRun

T = TypeVar("T")

MAX_PER_PAGE = 200

MSG_INVALID_PAGE = "page must be >= 1"
MSG_INVALID_PER_PAGE = f"per_page must be between 1 and {MAX_PER_PAGE}"


class MediaFileSort(StrEnum):
    """Columns a media-file listing can be ordered by."""

    PATH = auto()
    FILENAME = auto()
    SIZE_BYTES = auto()
    CREATION_DATE = auto()
    QUALITY_SCORE = auto()


class SortDirection(StrEnum):
    """Direction of an ``ORDER BY`` clause."""

    ASC = auto()
    DESC = auto()


class DuplicateStatus(StrEnum):
    """Whether a media file belongs to a duplicate group."""

    GROUPED = auto()
    UNGROUPED = auto()


_SORT_COLUMNS: dict[MediaFileSort, str] = {
    MediaFileSort.PATH: "media_files.path",
    MediaFileSort.FILENAME: "media_files.filename",
    MediaFileSort.SIZE_BYTES: "media_files.size_bytes",
    MediaFileSort.CREATION_DATE: "media_files.creation_date",
    MediaFileSort.QUALITY_SCORE: "media_files.quality_score",
}
_DIRECTIONS: dict[SortDirection, str] = {
    SortDirection.ASC: "ASC",
    SortDirection.DESC: "DESC",
}
_DUPLICATE_STATUS_CLAUSES: dict[DuplicateStatus, str] = {
    DuplicateStatus.GROUPED: "media_files.group_id IS NOT NULL",
    DuplicateStatus.UNGROUPED: "media_files.group_id IS NULL",
}

_MEDIA_FILES_FROM = "FROM media_files"
_MEDIA_FILES_FTS_FROM = (
    "FROM media_files JOIN media_files_fts ON media_files_fts.rowid = media_files.id"
)


@dataclass(frozen=True, slots=True)
class PageRequest:
    """One page of an offset-paginated listing.

    Args:
        page: 1-based page number.
        per_page: Number of items per page, at most :data:`MAX_PER_PAGE`.

    Raises:
        ValueError: If ``page`` is below 1 or ``per_page`` is outside
            ``1..MAX_PER_PAGE``.
    """

    page: int = 1
    per_page: int = 50

    def __post_init__(self) -> None:
        """Validate the page bounds."""
        if self.page < 1:
            raise ValueError(MSG_INVALID_PAGE)
        if not 1 <= self.per_page <= MAX_PER_PAGE:
            raise ValueError(MSG_INVALID_PER_PAGE)

    @property
    def offset(self) -> int:
        """Return the ``OFFSET`` of this page.

        Returns:
            Number of rows to skip before the first item of the page.
        """
        return (self.page - 1) * self.per_page


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of results together with the total match count.

    Args:
        items: Items on this page, in listing order.
        total: Total number of items matching the query across all pages.
        page: 1-based page number that was requested.
        per_page: Requested page size.
    """

    items: list[T]
    total: int
    page: int
    per_page: int


@dataclass(frozen=True, slots=True)
class MediaFileFilter:
    """Filters applied to a media-file listing; ``None`` means unfiltered.

    Args:
        run_id: Restrict to files of this run.
        media_type: Compared verbatim to ``media_files.media_type``.
        duplicate_status: Restrict to grouped or ungrouped files.
        created_from: Inclusive lower bound on ``creation_date`` in the
            stored ``YYYY-MM-DDTHH:MM:SSZ`` format.
        created_to: Exclusive upper bound on ``creation_date`` in the same
            format.
        search: Free-text FTS query over path, filename and metadata. A
            blank string is treated as no search.
    """

    run_id: int | None = None
    media_type: str | None = None
    duplicate_status: DuplicateStatus | None = None
    created_from: str | None = None
    created_to: str | None = None
    search: str | None = None

    @property
    def has_search(self) -> bool:
        """Return whether a non-blank full-text search is set.

        Returns:
            ``True`` when ``search`` contains at least one term.
        """
        return bool(self.search and self.search.strip())


@dataclass(frozen=True, slots=True)
class DuplicateGroupWithMembers:
    """A duplicate group together with its member media files.

    Args:
        group: The duplicate group row.
        members: The files assigned to the group, in path order.
    """

    group: DuplicateGroupRecord
    members: list[MediaFileRecord]


def _media_file_where(filters: MediaFileFilter) -> tuple[str, list[object]]:
    """Build the ``WHERE`` clause shared by the page query and its count.

    Args:
        filters: Filters to translate.

    Returns:
        The ``WHERE`` clause (empty when unfiltered, otherwise prefixed with
        a space) and the parameter values bound in it, in order.
    """
    clauses: list[str] = []
    params: list[object] = []
    if filters.run_id is not None:
        clauses.append("media_files.run_id = ?")
        params.append(filters.run_id)
    if filters.media_type is not None:
        clauses.append("media_files.media_type = ?")
        params.append(filters.media_type)
    if filters.duplicate_status is not None:
        clauses.append(_DUPLICATE_STATUS_CLAUSES[filters.duplicate_status])
    if filters.created_from is not None:
        clauses.append("media_files.creation_date >= ?")
        params.append(filters.created_from)
    if filters.created_to is not None:
        clauses.append("media_files.creation_date < ?")
        params.append(filters.created_to)
    if filters.has_search and filters.search is not None:
        clauses.append("media_files_fts MATCH ?")
        params.append(MediaStore._build_match_query(filters.search))
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _media_file_order_by(
    filters: MediaFileFilter,
    *,
    sort: MediaFileSort,
    direction: SortDirection,
) -> str:
    """Build the whitelisted ``ORDER BY`` clause of a media-file listing.

    Args:
        filters: Active filters; a search adds FTS ``rank`` as the second key.
        sort: Primary sort column.
        direction: Direction of the primary sort column.

    Returns:
        The ``ORDER BY`` clause, prefixed with a space.
    """
    keys = [f"{_SORT_COLUMNS[sort]} {_DIRECTIONS[direction]}"]
    if filters.has_search:
        keys.append("rank")
    keys.append("media_files.id ASC")
    return " ORDER BY " + ", ".join(keys)


def _count(db: ReportDatabase, sql: str, params: list[object]) -> int:
    """Run a ``COUNT(*)`` statement and return its single value.

    Args:
        db: Connected report database.
        sql: Count statement selecting the column ``n``.
        params: Bound parameter values.

    Returns:
        The counted number of rows.
    """
    rows = db._query(sql, params)
    return int(rows[0]["n"])


def list_runs_page(db: ReportDatabase, *, page: PageRequest) -> Page[ReportRun]:
    """List report runs, oldest first, one page at a time.

    Args:
        db: Connected report database.
        page: Page to return.

    Returns:
        The requested page of runs and the total run count.
    """
    total = _count(db, "SELECT COUNT(*) AS n FROM report_runs;", [])
    rows = db._query(
        "SELECT * FROM report_runs ORDER BY started_at ASC, id ASC LIMIT ? OFFSET ?;",
        (page.per_page, page.offset),
    )
    return Page(
        items=[ReportRun.from_row(row) for row in rows],
        total=total,
        page=page.page,
        per_page=page.per_page,
    )


def list_media_files_page(
    db: ReportDatabase,
    *,
    filters: MediaFileFilter,
    sort: MediaFileSort,
    direction: SortDirection,
    page: PageRequest,
) -> Page[MediaFileRecord]:
    """List media files matching ``filters``, sorted and paginated.

    The sort column and direction come from enum whitelists; ties are broken
    by FTS ``rank`` when a search is active, then by ``id`` ascending. The
    total is counted over the same ``WHERE`` clause as the page.

    Args:
        db: Connected report database.
        filters: Filters to apply.
        sort: Primary sort column.
        direction: Direction of the primary sort column.
        page: Page to return.

    Returns:
        The requested page of media files and the total match count.
    """
    source = _MEDIA_FILES_FTS_FROM if filters.has_search else _MEDIA_FILES_FROM
    where, params = _media_file_where(filters)
    order_by = _media_file_order_by(filters, sort=sort, direction=direction)
    total = _count(
        db,
        f"SELECT COUNT(*) AS n {source}{where};",  # nosec B608 - fixed fragments; values bound
        params,
    )
    rows = db._query(
        f"SELECT media_files.* {source}{where}{order_by} LIMIT ? OFFSET ?;",  # nosec B608 - whitelisted ORDER BY; values bound
        [*params, page.per_page, page.offset],
    )
    return Page(
        items=[MediaFileRecord.from_row(row) for row in rows],
        total=total,
        page=page.page,
        per_page=page.per_page,
    )


def _group_members(
    db: ReportDatabase,
    group_ids: list[int],
) -> dict[int, list[MediaFileRecord]]:
    """Fetch the member files of the given groups, keyed by group id.

    Args:
        db: Connected report database.
        group_ids: Identifiers of the groups on the current page.

    Returns:
        Mapping from group id to its members in path order; groups without
        members are absent.
    """
    members: dict[int, list[MediaFileRecord]] = defaultdict(list)
    if not group_ids:
        return members
    placeholders = ", ".join("?" for _ in group_ids)
    rows = db._query(
        "SELECT * FROM media_files "
        f"WHERE group_id IN ({placeholders}) "  # nosec B608 - placeholders only; ids bound
        "ORDER BY path ASC, id ASC;",
        group_ids,
    )
    for row in rows:
        record = MediaFileRecord.from_row(row)
        if record.group_id is not None:
            members[record.group_id].append(record)
    return members


def list_duplicate_groups_page(
    db: ReportDatabase,
    *,
    run_id: int | None,
    page: PageRequest,
) -> Page[DuplicateGroupWithMembers]:
    """List duplicate groups with their members, one page at a time.

    Groups are ordered by run, then group number. Members are loaded in one
    additional query for the groups on the page.

    Args:
        db: Connected report database.
        run_id: Restrict to groups of this run, or ``None`` for all runs.
        page: Page to return.

    Returns:
        The requested page of groups with members and the total group count.
    """
    where = "" if run_id is None else " WHERE run_id = ?"
    params: list[object] = [] if run_id is None else [run_id]
    total = _count(
        db,
        f"SELECT COUNT(*) AS n FROM duplicate_groups{where};",  # nosec B608 - fixed clause; value bound
        params,
    )
    rows = db._query(
        f"SELECT * FROM duplicate_groups{where} "  # nosec B608 - fixed clause; value bound
        "ORDER BY run_id ASC, group_number ASC, id ASC LIMIT ? OFFSET ?;",
        [*params, page.per_page, page.offset],
    )
    groups = [DuplicateGroupRecord.from_row(row) for row in rows]
    members = _group_members(db, [group.id for group in groups])
    return Page(
        items=[
            DuplicateGroupWithMembers(group=group, members=members.get(group.id, []))
            for group in groups
        ],
        total=total,
        page=page.page,
        per_page=page.per_page,
    )


__all__ = [
    "MAX_PER_PAGE",
    "DuplicateGroupWithMembers",
    "DuplicateStatus",
    "MediaFileFilter",
    "MediaFileSort",
    "Page",
    "PageRequest",
    "SortDirection",
    "list_duplicate_groups_page",
    "list_media_files_page",
    "list_runs_page",
]
