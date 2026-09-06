"""Apple Live Photo pair detection.

A Live Photo is stored as two files: a still (HEIC or JPEG) and a short MOV
clip. Apple links them through a shared *content identifier* kept in the
still's MakerNote (``Tag 0x0011``) and the QuickTime key
``com.apple.quicktime.content.identifier`` in the video. This module reads
those identifiers and groups candidate files into pairs.

:func:`find_live_photo_pairs` is pure: it accepts reader callables so the
pairing rules can be tested without EXIF or ffprobe. :func:`detect_live_photos`
is the filesystem-backed convenience wrapper.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from winnow.media.image import read_maker_note_tags
from winnow.media.video import read_video_tags

STILL_SUFFIXES: Final[frozenset[str]] = frozenset({".heic", ".heif", ".jpg", ".jpeg"})
VIDEO_SUFFIXES: Final[frozenset[str]] = frozenset({".mov"})
APPLE_CONTENT_IDENTIFIER_TAG: Final[str] = "Tag 0x0011"
QUICKTIME_CONTENT_IDENTIFIER_KEY: Final[str] = "com.apple.quicktime.content.identifier"

IdentifierReader = Callable[[Path], str | None]

_Candidates = list[tuple[Path, str | None]]


@dataclass(frozen=True, slots=True)
class LivePhotoPair:
    """A still and video that belong to the same Live Photo.

    Attributes:
        still: Path to the still image.
        video: Path to the companion video clip.
        content_identifier: Shared identifier; ``None`` only when ``verified``
            is ``False``.
        verified: ``True`` when both files carry the same content identifier;
            ``False`` when the pair was inferred from a shared stem alone.
    """

    still: Path
    video: Path
    content_identifier: str | None
    verified: bool


@dataclass(frozen=True, slots=True)
class LivePhotoScan:
    """Result of scanning a set of paths for Live Photo pairs.

    Attributes:
        pairs: Verified pairs first, then unverified; each group sorted by
            still path.
        unpaired_stills: Candidate stills without a companion, sorted.
        unpaired_videos: Candidate videos without a companion, sorted.
    """

    pairs: tuple[LivePhotoPair, ...]
    unpaired_stills: tuple[Path, ...]
    unpaired_videos: tuple[Path, ...]


def still_content_identifier(path: Path) -> str | None:
    """Read the Apple content identifier from a still image's MakerNote.

    Args:
        path: Filesystem path to the still.

    Returns:
        The identifier string, or ``None`` when absent, blank, or unreadable.
    """
    return _normalize_identifier(
        read_maker_note_tags(path).get(APPLE_CONTENT_IDENTIFIER_TAG),
    )


def video_content_identifier(path: Path) -> str | None:
    """Read the QuickTime content identifier from a video container.

    Args:
        path: Filesystem path to the video.

    Returns:
        The identifier string, or ``None`` when absent, blank, or ffprobe is
        missing.
    """
    return _normalize_identifier(
        read_video_tags(path).get(QUICKTIME_CONTENT_IDENTIFIER_KEY),
    )


def _normalize_identifier(value: str | None) -> str | None:
    """Strip an identifier and treat blank values as absent.

    Args:
        value: Raw tag value.

    Returns:
        The stripped identifier, or ``None`` when ``value`` is ``None`` or
        whitespace-only.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def find_live_photo_pairs(
    paths: Iterable[Path],
    *,
    still_identifier: IdentifierReader = still_content_identifier,
    video_identifier: IdentifierReader = video_content_identifier,
) -> LivePhotoScan:
    """Group candidate paths into Live Photo pairs.

    Pairing rules:

    1. Candidates are paths whose lower-cased suffix is in
       :data:`STILL_SUFFIXES` or :data:`VIDEO_SUFFIXES`. Identifiers are
       stripped; blank values are treated as ``None``.
    2. A still and a video with equal identifiers form a verified pair
       regardless of stem or directory. When several files share one
       identifier, same-directory-same-stem matches are paired first, then the
       remainder lexicographically; leftovers are orphans.
    3. A still and a video in the same directory with equal case-insensitive
       stems where at least one identifier is ``None`` form an unverified pair.
    4. Equal stems with different identifiers are not a pair.
    5. Everything else is an orphan.

    Args:
        paths: Paths to consider; non-candidates are ignored.
        still_identifier: Reader returning a still's content identifier.
        video_identifier: Reader returning a video's content identifier.

    Returns:
        The scan result with pairs and orphans in deterministic order.
    """
    stills, videos = _read_candidates(
        paths=paths,
        still_identifier=still_identifier,
        video_identifier=video_identifier,
    )
    verified, stills, videos = _pair_verified(stills=stills, videos=videos)
    unverified, stills, videos = _pair_by_stem(stills=stills, videos=videos)
    return LivePhotoScan(
        pairs=tuple(
            sorted(verified, key=_pair_key) + sorted(unverified, key=_pair_key),
        ),
        unpaired_stills=tuple(sorted(path for path, _ in stills)),
        unpaired_videos=tuple(sorted(path for path, _ in videos)),
    )


def detect_live_photos(directory: Path, *, recursive: bool = True) -> LivePhotoScan:
    """Scan a directory for Live Photo pairs.

    Args:
        directory: Root directory to scan.
        recursive: Descend into subdirectories when ``True``.

    Returns:
        The scan result for every candidate file under ``directory``.
    """
    entries = directory.rglob("*") if recursive else directory.iterdir()
    return find_live_photo_pairs(entry for entry in entries if entry.is_file())


def _read_candidates(
    *,
    paths: Iterable[Path],
    still_identifier: IdentifierReader,
    video_identifier: IdentifierReader,
) -> tuple[_Candidates, _Candidates]:
    """Filter paths to candidates and read each one's identifier.

    Args:
        paths: Paths to inspect.
        still_identifier: Reader for still identifiers.
        video_identifier: Reader for video identifiers.

    Returns:
        ``(stills, videos)`` as sorted ``(path, identifier)`` lists.
    """
    stills: _Candidates = []
    videos: _Candidates = []
    for path in sorted(set(paths)):
        suffix = path.suffix.lower()
        if suffix in STILL_SUFFIXES:
            stills.append((path, _normalize_identifier(still_identifier(path))))
        elif suffix in VIDEO_SUFFIXES:
            videos.append((path, _normalize_identifier(video_identifier(path))))
    return stills, videos


def _index_by_identifier(candidates: _Candidates) -> dict[str, list[Path]]:
    """Group candidate paths by their non-``None`` identifier.

    Args:
        candidates: ``(path, identifier)`` tuples; identifiers are already
            normalized, so blank values arrive as ``None``.

    Returns:
        Mapping of identifier to the paths carrying it, in input order.
    """
    index: defaultdict[str, list[Path]] = defaultdict(list)
    for path, identifier in candidates:
        if identifier is not None:
            index[identifier].append(path)
    return index


def _pair_verified(
    *,
    stills: _Candidates,
    videos: _Candidates,
) -> tuple[list[LivePhotoPair], _Candidates, _Candidates]:
    """Pair stills and videos that share a content identifier.

    Args:
        stills: Still candidates with identifiers.
        videos: Video candidates with identifiers.

    Returns:
        ``(pairs, remaining_stills, remaining_videos)``.
    """
    video_index = _index_by_identifier(videos)
    pairs: list[LivePhotoPair] = []
    used: set[Path] = set()
    for identifier, still_paths in _index_by_identifier(stills).items():
        matched = _match_bucket(
            stills=still_paths,
            videos=video_index.get(identifier, []),
        )
        for still, video in matched:
            pairs.append(
                LivePhotoPair(
                    still=still,
                    video=video,
                    content_identifier=identifier,
                    verified=True,
                ),
            )
            used.update((still, video))
    return (
        pairs,
        [item for item in stills if item[0] not in used],
        [item for item in videos if item[0] not in used],
    )


def _match_bucket(
    *,
    stills: list[Path],
    videos: list[Path],
) -> list[tuple[Path, Path]]:
    """Match stills to videos that all share one content identifier.

    Same-directory-same-stem matches are taken first; the remaining stills and
    videos are then zipped in lexicographic order. Leftovers stay unmatched.

    Args:
        stills: Sorted still paths sharing the identifier.
        videos: Sorted video paths sharing the identifier.

    Returns:
        ``(still, video)`` tuples for each match.
    """
    matched: list[tuple[Path, Path]] = []
    free_videos = list(videos)
    free_stills: list[Path] = []
    for still in stills:
        twin = next(
            (video for video in free_videos if _same_stem(still=still, video=video)),
            None,
        )
        if twin is None:
            free_stills.append(still)
            continue
        free_videos.remove(twin)
        matched.append((still, twin))
    matched.extend(zip(free_stills, free_videos, strict=False))
    return matched


def _pair_by_stem(
    *,
    stills: _Candidates,
    videos: _Candidates,
) -> tuple[list[LivePhotoPair], _Candidates, _Candidates]:
    """Pair leftover stills and videos by shared stem when an identifier is missing.

    Args:
        stills: Still candidates not paired by identifier.
        videos: Video candidates not paired by identifier.

    Returns:
        ``(unverified_pairs, remaining_stills, remaining_videos)``.
    """
    pairs: list[LivePhotoPair] = []
    used: set[Path] = set()
    for still, still_id in stills:
        match = next(
            (
                (video, video_id)
                for video, video_id in videos
                if video not in used
                and _same_stem(still=still, video=video)
                and (still_id is None or video_id is None)
            ),
            None,
        )
        if match is None:
            continue
        video, _ = match
        pairs.append(
            LivePhotoPair(
                still=still,
                video=video,
                content_identifier=None,
                verified=False,
            ),
        )
        used.update((still, video))
    return (
        pairs,
        [item for item in stills if item[0] not in used],
        [item for item in videos if item[0] not in used],
    )


def _same_stem(*, still: Path, video: Path) -> bool:
    """Report whether two paths share a directory and case-insensitive stem.

    Args:
        still: Still path.
        video: Video path.

    Returns:
        ``True`` when both live in the same directory with matching stems.
    """
    return still.parent == video.parent and still.stem.lower() == video.stem.lower()


def _pair_key(pair: LivePhotoPair) -> Path:
    """Sort key placing pairs in still-path order.

    Args:
        pair: Pair to key.

    Returns:
        The pair's still path.
    """
    return pair.still
