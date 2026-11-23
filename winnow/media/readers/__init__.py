"""Evidence readers (#395): each turns one file into observations.

Every reader implements :class:`~winnow.media.evidence.EvidenceReader` and
is passed to :func:`~winnow.media.evidence.collect_evidence`. Readers record
what a file says and decide nothing; the resolver (#396) decides.
"""

from __future__ import annotations

from winnow.media.readers.exif import ExifReader

__all__ = ["ExifReader"]
