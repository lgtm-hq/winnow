"""Media-file CRUD and full-text search for the report database.

:class:`MediaStore` provides the media-file helpers mixed into
:class:`winnow.report.database.ReportDatabase`, including the FTS5-backed
search over paths, filenames, and metadata text.
"""

from __future__ import annotations

from pathlib import Path

from winnow.models.media import MediaType
from winnow.report._connection import ConnectionManager
from winnow.report.records import MediaFileRecord


class MediaStore(ConnectionManager):
    """CRUD and search helpers for the ``media_files`` table."""

    def add_media_file(
        self,
        *,
        run_id: int,
        path: Path | str,
        media_type: MediaType | str,
        size_bytes: int = 0,
        content_hash: str | None = None,
        creation_date: str | None = None,
        group_id: int | None = None,
        filename: str | None = None,
        quality_score: float | None = None,
        metadata: str | None = None,
    ) -> int:
        """Insert a media file discovered during a run.

        Args:
            run_id: Owning run identifier.
            path: Path to the media file.
            media_type: Media type classification.
            size_bytes: File size in bytes.
            content_hash: Content hash for exact-duplicate detection, if any.
            creation_date: ISO-8601 creation timestamp, if known.
            group_id: Duplicate group identifier, if the file is a duplicate.
            filename: Explicit file name; derived from ``path`` when omitted.
            quality_score: Computed quality score of the file, if any.
            metadata: Searchable metadata text (EXIF summary, tags), if any.

        Returns:
            The identifier of the created media file.
        """
        resolved_name = filename if filename is not None else Path(path).name
        cursor = self._write(
            "INSERT INTO media_files "
            "(run_id, group_id, path, filename, media_type, size_bytes, "
            "content_hash, creation_date, quality_score, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                run_id,
                group_id,
                str(path),
                resolved_name,
                str(media_type),
                size_bytes,
                content_hash,
                creation_date,
                quality_score,
                metadata,
            ),
        )
        return self._last_row_id(cursor, operation="add_media_file")

    def get_media_file(self, file_id: int) -> MediaFileRecord | None:
        """Fetch a media file by identifier.

        Args:
            file_id: Identifier of the media file to fetch.

        Returns:
            The matching media file, or ``None`` if none exists.
        """
        rows = self._query(
            "SELECT * FROM media_files WHERE id = ?;",
            (file_id,),
        )
        return MediaFileRecord.from_row(rows[0]) if rows else None

    def list_media_files(self, run_id: int) -> list[MediaFileRecord]:
        """List media files for a run ordered by path.

        Args:
            run_id: Identifier of the owning run.

        Returns:
            The run's media files, in path order.
        """
        rows = self._query(
            "SELECT * FROM media_files WHERE run_id = ? ORDER BY path ASC, id ASC;",
            (run_id,),
        )
        return [MediaFileRecord.from_row(row) for row in rows]

    def assign_media_file_group(
        self,
        file_id: int,
        *,
        group_id: int | None,
    ) -> bool:
        """Assign or clear the duplicate group of a media file.

        The composite foreign key on ``media_files`` guarantees the group
        belongs to the same run as the file.

        Args:
            file_id: Identifier of the media file to update.
            group_id: Duplicate group identifier, or ``None`` to clear it.

        Returns:
            ``True`` if a media file was updated, ``False`` otherwise.

        Raises:
            ReportError: If the group does not exist in the file's run.
        """
        cursor = self._write(
            "UPDATE media_files SET group_id = ? WHERE id = ?;",
            (group_id, file_id),
        )
        return cursor.rowcount > 0

    def update_media_file_metadata(
        self,
        file_id: int,
        *,
        metadata: str | None,
    ) -> bool:
        """Replace the searchable metadata text of a media file.

        The FTS synchronization trigger reindexes the row so subsequent
        searches reflect the new metadata.

        Args:
            file_id: Identifier of the media file to update.
            metadata: New metadata text, or ``None`` to clear it.

        Returns:
            ``True`` if a media file was updated, ``False`` otherwise.
        """
        cursor = self._write(
            "UPDATE media_files SET metadata = ? WHERE id = ?;",
            (metadata, file_id),
        )
        return cursor.rowcount > 0

    @staticmethod
    def _build_match_query(query: str) -> str:
        """Convert free-text into a safe FTS5 term query.

        Whitespace-separated terms are wrapped as quoted phrases and combined
        with an implicit AND. Quoting escapes FTS5 operators (including
        characters such as ``.``) so that ordinary filename fragments do not
        raise syntax errors.

        Args:
            query: Free-text query to convert.

        Returns:
            A quoted FTS5 match expression covering all terms.
        """
        terms = query.split()
        return " ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

    def search_media_files(
        self,
        query: str,
        *,
        run_id: int | None = None,
        raw: bool = False,
    ) -> list[MediaFileRecord]:
        """Full-text search media files by path, filename, or metadata.

        Args:
            query: Search text. Treated as space-separated literal terms unless
                ``raw`` is set, in which case it is passed through as an FTS5
                match expression.
            run_id: Restrict results to a single run when provided.
            raw: Pass ``query`` directly as an FTS5 match expression.

        Returns:
            Matching media files ordered by FTS relevance. An empty query
            yields no results.
        """
        match_query = query if raw else self._build_match_query(query)
        if not match_query.strip():
            return []
        sql = (
            "SELECT media_files.* FROM media_files_fts "
            "JOIN media_files ON media_files.id = media_files_fts.rowid "
            "WHERE media_files_fts MATCH ?"
        )
        params: list[object] = [match_query]
        if run_id is not None:
            sql += " AND media_files.run_id = ?"
            params.append(run_id)
        sql += " ORDER BY rank;"
        rows = self._query(sql, params)
        return [MediaFileRecord.from_row(row) for row in rows]


__all__ = ["MediaStore"]
