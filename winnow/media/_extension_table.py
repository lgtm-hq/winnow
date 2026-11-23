"""winnow's own extension table for the name-based fallback (#464).

When no content rule recognises a file, detection falls back to its
extension (rule 6 of :mod:`winnow.media._detect`). That answer comes from
this table, which ships with winnow, so it is the same on every host and
every Python version. The host's ``mimetypes`` table, which reads system
files such as ``/etc/mime.types`` and differs between machines and Python
releases, is consulted only when a registry asks for it
(``FormatRegistry(use_mime_fallback=True)``).

The table names a media type for an extension that names one format, or
one family of formats, whose media type is image, audio or video. Each
group below cites its source: CPython's built-in table
(``Lib/mimetypes.py``, ``_types_map_default`` in 3.15), the IANA media type
registry, or the format's own specification. An extension that names
formats of different media types, or a common non-media format, is left
out (see :data:`EXCLUDED_EXTENSIONS`); a file with such an extension and
unrecognised content is unknown. Playlist extensions
(:data:`~winnow.media._format_checks.PLAYLIST_EXTENSIONS`) are never in
the table: a playlist is not media.

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

_GROUPS: Final[tuple[tuple[MediaType, str], ...]] = (
    # --- CPython's built-in table (Lib/mimetypes.py, 3.15), image/* ---------
    (
        _IMAGE,
        "avif bmp emf fits g3 gif heic heif ico ief jp2 jpe jpeg jpg jpm jpx jxl"
        " pbm pgm png pnm ppm ras rgb svg t38 tfx tif tiff webp wmf xbm xpm xwd",
    ),
    # CPython's suffix_map reads .svgz as .svg.gz, which its table types
    # image/svg+xml with gzip encoding.
    (_IMAGE, "svgz"),
    # --- CPython's built-in table (Lib/mimetypes.py, 3.15), audio/* ---------
    # .ass is left out though CPython maps it to audio/aac: it names the
    # Advanced SubStation Alpha subtitle format (EXCLUDED_EXTENSIONS).
    (
        _AUDIO,
        "aac adts aif aifc aiff au flac loas m4a mka mp2 mp3 ogg opus ra snd"
        " wav weba",
    ),
    # --- CPython's built-in table (Lib/mimetypes.py, 3.15), video/* ---------
    # .mpa is video/mpeg there, and stays so here.
    (
        _VIDEO,
        "avi m1v m4v mk3d mkv mov movie mp4 mpa mpe mpeg mpg ogv qt webm wmv",
    ),
    # 3GPP and 3GPP2 files: RFC 3839 and RFC 4393 register both an audio and
    # a video type for .3gp and .3g2; the video type is the one for files
    # that hold video. winnow takes video, as for the other ISO-BMFF
    # movie extensions (CPython's built-in table says audio/3gpp).
    (_VIDEO, "3g2 3gp 3gpp 3gpp2"),
    # --- Images winnow's signature table recognises (_signatures.py) -------
    # JPEG File Interchange Format 1.02; PNG (ISO/IEC 15948) and APNG.
    (_IMAGE, "jfif apng"),
    # Microsoft ICONDIR type 2 (cursor).
    (_IMAGE, "cur"),
    # RFC 3745: image/jp2 and image/jpx files; .j2k and .j2c hold the raw
    # codestream (ISO/IEC 15444-1, Annex A).
    (_IMAGE, "j2c j2k jpf jpg2"),
    # Netpbm PAM; SGI image (The SGI Image File Format 1.00); Sun raster.
    (_IMAGE, "pam sgi rgba bw sun"),
    # ZSoft PCX and DCX; DirectDraw Surface; Apple icon image; OpenEXR.
    (_IMAGE, "pcx dcx dds icns exr"),
    # GIMP XCF; JPEG XR (IANA image/jxr: .jxr, .wdp, .hdp); IFF ILBM.
    (_IMAGE, "xcf jxr wdp hdp ilbm lbm"),
    # FITS (RFC 4047, image/fits: .fits, .fit, .fts); DjVu (IANA
    # image/vnd.djvu); Photoshop PSD and PSB (Adobe Photoshop File Formats
    # Specification).
    (_IMAGE, "fit fts djvu djv psd psb"),
    # Canon CRW (CIFF 1.0r4); JPEG XS (RFC 9134, image/jxs); DPX (SMPTE
    # 268M, IANA image/dpx); QOI 1.0; JNG 1.0; MODI (IANA image/vnd.ms-modi).
    (_IMAGE, "crw jxs dpx qoi jng mdi"),
    # QuickTime image file (Apple QuickTime File Format, image files);
    # OpenRaster (ora specification).
    (_IMAGE, "qtif qti ora"),
    # --- ISO-BMFF image brands (_detect_bmff.py) ---------------------------
    # ISO/IEC 23008-12 and IANA image/heic-sequence, image/heif-sequence;
    # .hif as cameras write HEIF; AVIF image sequences (AV1 Image File Format).
    (_IMAGE, "heics heifs hif avifs"),
    # --- RAW camera formats (registry.RAW_IMAGE_MIME_TYPES) ----------------
    # Camera makers' own extensions, as ExifTool and LibRaw document them.
    (
        _IMAGE,
        "3fr ari arw cr2 cr3 dcr dng erf k25 kdc mrw nef nrw orf pef raf raw"
        " rw2 rwl sr2 srf srw x3f",
    ),
    # --- Extension-only formats (registry.EXTENSION_ONLY_FORMATS, #393) ----
    # Typed by name here; detection still requires the format's marker.
    (
        _IMAGE,
        "ag ai dib dwg dxf eps epsf epsi fig fpx pct pic pict sk sk1 tga",
    ),
    (_AUDIO, "669 koz"),
    (_VIDEO, "fli flc"),
    # --- Audio winnow's signature table recognises (_signatures.py) --------
    # RIFF WAVE, EBU Tech 3285 (BWF) and 3306 (RF64); Sony Wave64.
    (_AUDIO, "wave bwf rf64 w64"),
    # Apple Core Audio Format; WavPack; True Audio; Creative Voice.
    (_AUDIO, "caf wv tta voc"),
    # Monkey's Audio; Musepack SV7 and SV8.
    (_AUDIO, "ape mpc mp+"),
    # Standard MIDI Files 1.0 (and RIFF RMID, karaoke .kar).
    (_AUDIO, "mid midi kar rmi"),
    # RFC 4867 (audio/AMR, audio/AMR-WB); RFC 3625 (QCELP, .qcp).
    (_AUDIO, "amr awb qcp"),
    # IFF 8SVX; Scream Tracker 2 and 3; FastTracker 2 modules and
    # instruments; Impulse Tracker; OctaMED; MultiTracker; UltraTracker; MO3.
    (_AUDIO, "8svx stm s3m xm xi it med mtm ult mo3"),
    # Extensible Music Format (RFC 4723, audio/mobile-xmf: .mxmf).
    (_AUDIO, "xmf mxmf"),
    # MPEG audio (RFC 3003; .mpga as Apache httpd's mime.types lists it);
    # AC-3 (RFC 4184); E-AC-3 (RFC 4598); DTS (IANA audio/vnd.dts).
    (_AUDIO, "mp1 mpga ac3 eac3 ec3 dts"),
    # Ogg audio (RFC 5334: .oga, .spx).
    (_AUDIO, "oga spx"),
    # ISO-BMFF audio brands M4B, M4P, F4A, F4B (_detect_bmff.AUDIO_BRANDS).
    (_AUDIO, "m4b m4p f4a f4b"),
    # Windows Media Audio in ASF.
    (_AUDIO, "wma"),
    # Ogg Media (OGM), a video container in Ogg pages.
    (_VIDEO, "ogm"),
    # --- Video winnow's signature table recognises (_signatures.py) --------
    # ASF specification 3.1; Adobe FLV and F4V (Video File Format
    # Specification 10.1).
    (_VIDEO, "asf flv f4v f4p"),
    # MPEG-2 video and program streams (ISO/IEC 13818-1 and -2), DVD video
    # objects; MPEG-2 transport streams (IANA video/MP2T; .mts and .m2ts
    # as AVCHD cameras write them). .ts is left out (EXCLUDED_EXTENSIONS).
    (_VIDEO, "m2v vob mts m2ts m2t"),
    # Raw DV (IEC 61834-2); YUV4MPEG2; MNG 1.0, typed video as its content
    # rule types it. .dif is left out (EXCLUDED_EXTENSIONS).
    (_VIDEO, "dv y4m mng"),
    # Bink, Smacker, Nullsoft Streaming Video.
    (_VIDEO, "bik bk2 smk nsv"),
    # Motion JPEG 2000 (RFC 3745, video/mj2); Canon CRM movies
    # (https://github.com/lclevy/canon_cr3).
    (_VIDEO, "mj2 mjp2 crm"),
)

EXCLUDED_EXTENSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ass": "Advanced SubStation Alpha subtitles",
        "dat": "Video CD MPEG or any data file",
        "dif": "raw DV video or a text diff",
        "mod": "tracker module audio or JVC camcorder MPEG video",
        "nif": "Navy Image File Format or NetImmerse 3D model",
        "psf": "Portable Sound Format audio or PC Screen Font",
        "rm": "RealMedia, registered only as application/vnd.rn-realmedia",
        "rmvb": "RealMedia, registered only as application/vnd.rn-realmedia-vbr",
        "sid": "Commodore 64 SID audio or MrSID image",
        "ts": "MPEG transport stream or TypeScript source",
    },
)
"""Extensions left out of the table on purpose, and why.

Each names more than one format, or a format registered under no image,
audio or video type. A file with one of these extensions is typed only by
its content.
"""


def _build() -> dict[str, MediaType]:
    """Merge the groups into one mapping.

    Returns:
        Lower-case extension without the dot mapped to its media type.

    Raises:
        ValueError: If an extension appears twice.
    """
    table: dict[str, MediaType] = {}
    for media_type, extensions in _GROUPS:
        for extension in extensions.split():
            if extension in table:
                raise ValueError(f"extension listed twice: {extension}")
            table[extension] = media_type
    return table


EXTENSION_MEDIA_TYPES: Final[Mapping[str, MediaType]] = MappingProxyType(_build())
"""winnow's extension table: lower-case extension, no dot, to media type."""

__all__ = [
    "EXCLUDED_EXTENSIONS",
    "EXTENSION_MEDIA_TYPES",
    "EXTENSION_TABLE_NAME",
    "EXTENSION_TABLE_VERSION",
]
