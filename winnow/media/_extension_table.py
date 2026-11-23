"""winnow's own extension table for the name-based fallback (#464).

When no content rule recognises a file, detection falls back to its
extension (rule 6 of :mod:`winnow.media._detect`). That answer comes from
this table, which ships with winnow, so it is the same on every host and
every Python version. The host's ``mimetypes`` table, which reads system
files such as ``/etc/mime.types`` and differs between machines and Python
releases, is consulted only when a registry asks for it
(``FormatRegistry(use_mime_fallback=True)``).

The table only makes the existing name answers host-independent; it never
makes a file media by name that winnow did not already type by name. An
extension is in the table only under one of these rules:

1. **Stable before.** CPython's built-in table (``mimetypes.MimeTypes``
   with no host files) gives it the same media type in every supported
   Python, 3.11 to 3.15, or it is one of the registry's own formats
   (``registry.RAW_IMAGE_MIME_TYPES`` and
   ``registry.EXTENSION_ONLY_FORMATS``). It keeps exactly that type, even
   where the type is debatable; changing a stable answer is a separate
   decision.
2. **Host- or version-dependent before, one answer now.** A host MIME file
   or a newer Python typed it as media and the built-in table of an older
   Python did not (or typed it differently). It is in the table only if it
   names a single media format with no credible competing use *and* winnow
   has a content rule for that format, so a real file is recognised by its
   content anyway and the name only fills in for unreadable or
   unrecognised content. These are :data:`HOST_DEPENDENT_ADDITIONS`, each
   with its format and content rule. The others are
   :data:`EXCLUDED_EXTENSIONS`, each with the reason.

Any extension no supported Python typed as media by name stays out of the
table, even when a content rule recognises its format: content still types
such a file, the name alone does not. Playlist extensions
(:data:`~winnow.media._format_checks.PLAYLIST_EXTENSIONS`) are never in the
table.

Any change to an entry's answer raises :data:`EXTENSION_TABLE_VERSION`,
which detection records in the extension signal (``table=winnow/1``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from winnow.models.media import MediaType

EXTENSION_TABLE_VERSION: Final[int] = 1
"""Version of :data:`EXTENSION_MEDIA_TYPES`; raised whenever an answer changes."""

EXTENSION_TABLE_NAME: Final[str] = f"winnow/{EXTENSION_TABLE_VERSION}"
"""Name of this table in the extension signal's ``table=`` field."""

_IMAGE = MediaType.IMAGE
_AUDIO = MediaType.AUDIO
_VIDEO = MediaType.VIDEO

_STABLE_GROUPS: Final[tuple[tuple[MediaType, str], ...]] = (
    # --- Rule 1: CPython's built-in table, the same in 3.11 to 3.15 --------
    # (Lib/mimetypes.py, _types_map_default; .svgz through its suffix_map,
    # as .svg.gz). Kept exactly as CPython types them: .3gp, .3g2, .3gpp
    # and .3gpp2 are audio/3gpp*, .ass is audio/aac, .mpa is video/mpeg.
    (
        _IMAGE,
        "avif bmp gif heic heif ico ief jpe jpeg jpg pbm pgm png pnm ppm ras"
        " rgb svg svgz tif tiff xbm xpm xwd",
    ),
    (
        _AUDIO,
        "3g2 3gp 3gpp 3gpp2 aac adts aif aifc aiff ass au loas mp2 mp3 opus ra"
        " snd wav",
    ),
    (_VIDEO, "avi m1v mov movie mp4 mpa mpe mpeg mpg qt webm"),
    # --- Rule 1: the registry's own formats --------------------------------
    # RAW camera extensions (registry.RAW_IMAGE_MIME_TYPES).
    (
        _IMAGE,
        "3fr ari arw cr2 cr3 dcr dng erf k25 kdc mrw nef nrw orf pef raf raw"
        " rw2 rwl sr2 srf srw x3f",
    ),
    # Extension-only formats (registry.EXTENSION_ONLY_FORMATS, #393);
    # detection still requires the format's marker in readable content.
    (
        _IMAGE,
        "ag ai dib dwg dxf eps epsf epsi fig fpx jxl pct pic pict sk sk1 tga",
    ),
    (_AUDIO, "669 koz"),
    (_VIDEO, "fli flc"),
)

HOST_DEPENDENT_ADDITIONS: Final[Mapping[str, MediaType]] = MappingProxyType(
    {
        # --- Rule 2: audio --------------------------------------------------
        # FLAC (RFC 9639); content rule: _header_checks.flac.
        "flac": _AUDIO,
        # MPEG-4 audio and protected audio; content rule: ISO-BMFF
        # (_detect_bmff, brands M4A and M4P).
        "m4a": _AUDIO,
        "m4p": _AUDIO,
        # Standard MIDI File and RIFF MIDI; content rules:
        # _header_checks.midi and the RIFF RMID signature. (.mid is left
        # out, see EXCLUDED_EXTENSIONS.)
        "midi": _AUDIO,
        "rmi": _AUDIO,
        # Matroska audio and WebM audio; content rule: _matroska.
        "mka": _AUDIO,
        "weba": _AUDIO,
        # MPEG audio (RFC 3003); content rule: consecutive MPEG audio frames.
        "mpga": _AUDIO,
        # Ogg audio (RFC 5334: .ogg and .oga); content rule: Ogg streams.
        "ogg": _AUDIO,
        "oga": _AUDIO,
        # Scream Tracker 3 and FastTracker 2 modules; content rules:
        # _format_checks.scream_tracker_3 and fasttracker.
        "s3m": _AUDIO,
        "xm": _AUDIO,
        # Windows Media Audio; content rule: ASF stream declarations.
        "wma": _AUDIO,
        # --- Rule 2: image --------------------------------------------------
        # DjVu (IANA image/vnd.djvu); content rule: _header_checks.djvu.
        "djv": _IMAGE,
        "djvu": _IMAGE,
        # Enhanced and Windows metafiles (RFC 7903); content rules:
        # _format_checks.emf and wmf.
        "emf": _IMAGE,
        "wmf": _IMAGE,
        # HEIF and HEIC image sequences (ISO/IEC 23008-12); content rule:
        # ISO-BMFF image-sequence brands.
        "heics": _IMAGE,
        "heifs": _IMAGE,
        # JPEG 2000 (RFC 3745); content rule: the JP2 signature box.
        "jp2": _IMAGE,
        # ZSoft PCX; content rule: _header_checks.pcx.
        "pcx": _IMAGE,
        # Adobe Photoshop; content rule: _format_checks.psd.
        "psd": _IMAGE,
        # QuickTime image file; content rule: _format_checks.quicktime_image.
        "qtif": _IMAGE,
        # SGI image; content rule: _header_checks.sgi.
        "sgi": _IMAGE,
        # JPEG XR / HD Photo (IANA image/jxr); content rule: the JPEG XR
        # signature.
        "wdp": _IMAGE,
        # WebP (RFC 9649); content rule: RIFF form type WEBP.
        "webp": _IMAGE,
        # --- Rule 2: video --------------------------------------------------
        # ASF and Windows Media Video; content rule: ASF stream declarations.
        "asf": _VIDEO,
        "wmv": _VIDEO,
        # Raw DV (IEC 61834-2); content rule: _header_checks.dv.
        "dv": _VIDEO,
        # Flash video (Adobe FLV and F4V specification 10.1); content rules:
        # FLV stream declarations and ISO-BMFF.
        "flv": _VIDEO,
        "f4v": _VIDEO,
        # MPEG-2 transport streams as Blu-ray and HDV write them; content
        # rule: _mpegts. (.ts and .mts are left out, see EXCLUDED_EXTENSIONS.)
        "m2t": _VIDEO,
        "m2ts": _VIDEO,
        # MPEG-2 video elementary stream; content rule:
        # _header_checks.mpeg_video.
        "m2v": _VIDEO,
        # MPEG-4 video; content rule: ISO-BMFF.
        "m4v": _VIDEO,
        # Matroska video and 3D video (RFC 9559); content rule: _matroska.
        "mkv": _VIDEO,
        "mk3d": _VIDEO,
        # MNG, typed video as its content rule types it (_format_checks.mng).
        "mng": _VIDEO,
        # Ogg video (RFC 5334); content rule: Ogg streams.
        "ogv": _VIDEO,
    },
)
"""Rule 2 entries: host- or version-dependent names given one answer."""

EXCLUDED_EXTENSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "caf": "Core Audio Format or a Cal3D animation",
        "dif": "raw DV video, a text diff or a spreadsheet DIF file",
        "dts": "DTS audio or Devicetree source",
        "fits": "FITS also holds tables and other non-image data",
        "g3": "G3 fax, for which winnow has no content rule",
        "jpm": "JPEG 2000 compound image in some tables, video/jpm in others",
        "jpx": "JPEG 2000 extended image or a JBuilder project",
        "kar": "MIDI karaoke; other uses not ruled out",
        "mdi": "Microsoft Document Imaging; other uses not ruled out",
        "mid": "Standard MIDI File or MapInfo Interchange attribute data",
        "mj2": "Motion JPEG 2000, which the content rule types as an image",
        "mjp2": "Motion JPEG 2000, which the content rule types as an image",
        "mts": "AVCHD transport stream or a TypeScript module",
        "qti": "QuickTime image or a QtiPlot document",
        "sid": "Commodore 64 SID audio or a MrSID image",
        "spx": "Ogg Speex audio or an Apple System Profiler report",
        "t38": "T.38 fax, for which winnow has no content rule",
        "tfx": "TIFF-FX fax or a Pro Tools plug-in preset",
        "ts": "MPEG transport stream or TypeScript source",
        "vob": "DVD video object, a Vue object or a Live for Speed asset",
    },
)
"""Host- or version-dependent media names rule 2 leaves out, and why.

A file with one of these extensions is typed only by its content.
"""


def _build() -> dict[str, MediaType]:
    """Merge the rule 1 groups and the rule 2 additions into one mapping.

    Returns:
        Lower-case extension without the dot mapped to its media type.

    Raises:
        ValueError: If an extension appears twice or is also excluded.
    """
    table: dict[str, MediaType] = {}
    entries = [
        (extension, media_type)
        for media_type, extensions in _STABLE_GROUPS
        for extension in extensions.split()
    ]
    entries.extend(HOST_DEPENDENT_ADDITIONS.items())
    for extension, media_type in entries:
        if extension in table or extension in EXCLUDED_EXTENSIONS:
            raise ValueError(f"extension listed twice or excluded: {extension}")
        table[extension] = media_type
    return table


EXTENSION_MEDIA_TYPES: Final[Mapping[str, MediaType]] = MappingProxyType(_build())
"""winnow's extension table: lower-case extension, no dot, to media type."""

__all__ = [
    "EXCLUDED_EXTENSIONS",
    "EXTENSION_MEDIA_TYPES",
    "EXTENSION_TABLE_NAME",
    "EXTENSION_TABLE_VERSION",
    "HOST_DEPENDENT_ADDITIONS",
]
