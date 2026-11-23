"""Extensible media format registry and layered media-type detection.

Media types are resolved by :func:`winnow.media._detect.detect`, most
authoritative rule first: user overrides registered on a
:class:`FormatRegistry`, the file's content (container structure and
signatures, with ``puremagic`` as advisory evidence), then name-based
inference.

The name lookup answers from the registry's own formats (user registrations
and, with ``include_defaults``, the RAW and extension-only supplements), then
from winnow's versioned extension table
(:mod:`winnow.media._extension_table`), so the default answer is the same on
every host and Python version (#464). The host's ``mimetypes`` table, which
reads system files and differs between machines, is asked last and only when
the registry is built with ``use_mime_fallback=True``. That path uses a
private ``mimetypes.MimeTypes`` instance; importing winnow never changes the
process-wide ``mimetypes`` table.
"""

from __future__ import annotations

import functools
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from threading import Lock
from types import MappingProxyType
from typing import Final, Self

from winnow.media import _detect
from winnow.media._extension_table import EXTENSION_MEDIA_TYPES, EXTENSION_TABLE_NAME
from winnow.models.media import MediaType

DEFAULT_USE_MIME_FALLBACK: Final[bool] = False
"""Whether registries ask the host's ``mimetypes`` table unless told otherwise.

The one switch for the default of ``use_mime_fallback`` in
:class:`FormatRegistry`, :meth:`FormatRegistry.from_config` and
:func:`create_default_format_registry`. ``False`` keeps the name fallback
the same on every host (#464).
"""

REGISTRY_TABLE: Final[str] = "registry"
"""``table=`` value when the registry's own formats answered."""

HOST_MIME_TABLE: Final[str] = "host-mime"
"""``table=`` value when the host's ``mimetypes`` table answered."""

NO_TABLE: Final[str] = "none"
"""``table=`` value when no table the registry consults knows the extension."""

RAW_IMAGE_MIME_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "3fr": "image/x-hasselblad-3fr",
        "ari": "image/x-arriflex-ari",
        "arw": "image/x-sony-arw",
        "cr2": "image/x-canon-cr2",
        "cr3": "image/x-canon-cr3",
        "dcr": "image/x-kodak-dcr",
        "dng": "image/x-adobe-dng",
        "erf": "image/x-epson-erf",
        "k25": "image/x-kodak-k25",
        "kdc": "image/x-kodak-kdc",
        "mrw": "image/x-minolta-mrw",
        "nef": "image/x-nikon-nef",
        "nrw": "image/x-nikon-nrw",
        "orf": "image/x-olympus-orf",
        "pef": "image/x-pentax-pef",
        "raf": "image/x-fuji-raf",
        "raw": "image/x-panasonic-raw",
        "rw2": "image/x-panasonic-rw2",
        "rwl": "image/x-leica-rwl",
        "sr2": "image/x-sony-sr2",
        "srf": "image/x-sony-srf",
        "srw": "image/x-samsung-srw",
        "x3f": "image/x-sigma-x3f",
    },
)
"""RAW camera extensions and the MIME types the opt-in host lookup gives them."""

EXTENSION_ONLY_FORMATS: Mapping[str, MediaType] = MappingProxyType(
    {
        "669": MediaType.AUDIO,
        "koz": MediaType.AUDIO,
        "fli": MediaType.VIDEO,
        "flc": MediaType.VIDEO,
        "pict": MediaType.IMAGE,
        "pct": MediaType.IMAGE,
        "pic": MediaType.IMAGE,
        "tga": MediaType.IMAGE,
        "dib": MediaType.IMAGE,
        "fpx": MediaType.IMAGE,
        "jxl": MediaType.IMAGE,
        "eps": MediaType.IMAGE,
        "epsf": MediaType.IMAGE,
        "epsi": MediaType.IMAGE,
        "ai": MediaType.IMAGE,
        "dxf": MediaType.IMAGE,
        "dwg": MediaType.IMAGE,
        "fig": MediaType.IMAGE,
        "sk": MediaType.IMAGE,
        "sk1": MediaType.IMAGE,
        "ag": MediaType.IMAGE,
    },
)
"""Extension-only formats (#393, ruling item 2) and the type ``main`` gave each.

They have no magic detection can rely on alone (FLIC, PICT, 669, TGA
without a footer, a bare JPEG XL codestream, a raw DIB, FlashPix's shared
OLE container) or are drawings and documents (DWG, DXF, xfig, Skencil,
Applix Graphics, EPS and AI without ``EPSF``). Detection types such a file
only when its extension and the format's marker agree
(:mod:`winnow.media._extension_only`); a name lookup here gives the type
from the extension alone, so ``winnow info`` and ``winnow stats``, which
count by name, count these extensions as media whatever the content
(#366).
"""

DEFAULT_FORMATS: Mapping[str, MediaType] = MappingProxyType(
    {
        **dict.fromkeys(RAW_IMAGE_MIME_TYPES, MediaType.IMAGE),
        **EXTENSION_ONLY_FORMATS,
    },
)
"""Supplement seeded into default registries: RAW and extension-only formats.

Mainstream formats (JPEG, PNG, MP4, MP3, ...) are intentionally absent; they
resolve through content sniffing or winnow's extension table
(:data:`~winnow.media._extension_table.EXTENSION_MEDIA_TYPES`). The defaults
are name fallbacks, never overrides.
"""

_MIME_PREFIX_TYPES: Mapping[str, MediaType] = MappingProxyType(
    {
        "audio": MediaType.AUDIO,
        "image": MediaType.IMAGE,
        "video": MediaType.VIDEO,
    },
)


@dataclass(frozen=True, slots=True)
class ExtensionLookup:
    """An extension's media type and the table that gave it.

    Args:
        media_type: Media type the extension names, or ``None``.
        table: Which table answered: :data:`REGISTRY_TABLE`, winnow's
            extension table (``winnow/<version>``), :data:`HOST_MIME_TABLE`,
            or :data:`NO_TABLE` when none knows the extension.
    """

    media_type: MediaType | None
    table: str


class FormatRegistry:
    """Map media file extensions to supported media types.

    Args:
        formats: Optional custom extension mapping. Values may be ``MediaType``
            instances or media type strings such as ``"image"``.
        include_defaults: Whether to seed the registry with Winnow's built-in
            common media formats before applying custom formats, and to
            answer from winnow's extension table.
        use_mime_fallback: Whether ``lookup`` should ask the host's
            ``mimetypes`` table when no other table knows an extension. The
            answer then depends on the host; defaults to
            :data:`DEFAULT_USE_MIME_FALLBACK`.
    """

    __slots__ = (
        "_extension_map",
        "_include_defaults",
        "_lock",
        "_override_keys",
        "use_mime_fallback",
    )

    def __init__(
        self,
        formats: Mapping[str, MediaType | str] | None = None,
        *,
        include_defaults: bool = True,
        use_mime_fallback: bool = DEFAULT_USE_MIME_FALLBACK,
    ) -> None:
        self._extension_map: dict[str, MediaType] = {}
        self._override_keys: set[str] = set()
        self._lock = Lock()
        self._include_defaults = include_defaults
        self.use_mime_fallback = use_mime_fallback

        if include_defaults:
            self.register_many(DEFAULT_FORMATS, as_override=False)
        if formats is not None:
            self.register_many(formats)

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, object] | None = None,
    ) -> Self:
        """Build a registry from a config-file mapping.

        Args:
            config: Mapping shaped like the future config-file section. The
                supported keys are ``formats`` (extension mapping whose values
                are ``MediaType`` instances or strings), ``include_defaults``
                (``bool``), and ``use_mime_fallback`` (``bool``).

        Returns:
            Registry populated from default and custom extension mappings.
        """
        config_mapping = {} if config is None else config
        formats = _coerce_config_formats(config_mapping.get("formats", {}))
        include_defaults = _coerce_config_bool(
            config=config_mapping,
            key="include_defaults",
            default=True,
        )
        use_mime_fallback = _coerce_config_bool(
            config=config_mapping,
            key="use_mime_fallback",
            default=DEFAULT_USE_MIME_FALLBACK,
        )
        return cls(
            formats=formats,
            include_defaults=include_defaults,
            use_mime_fallback=use_mime_fallback,
        )

    @property
    def formats(self) -> Mapping[str, MediaType]:
        """Return a read-only view of registered extension mappings.

        Returns:
            Normalized extension names without leading dots mapped to media
            types.
        """
        with self._lock:
            return MappingProxyType(dict(self._extension_map))

    def register(
        self,
        *,
        extension: str,
        media_type: MediaType | str,
    ) -> None:
        """Register or replace an extension mapping.

        Args:
            extension: File extension, with or without a leading dot.
            media_type: Media type for files with this extension.

        Raises:
            ValueError: If the extension is empty or the media type is invalid.
        """
        normalized_extension = normalize_extension(extension)
        if not normalized_extension:
            raise ValueError("extension must contain at least one non-dot character")

        coerced_media_type = _coerce_media_type(media_type)
        with self._lock:
            self._extension_map[normalized_extension] = coerced_media_type
            self._override_keys.add(normalized_extension)

    def register_many(
        self,
        formats: Mapping[str, MediaType | str],
        *,
        as_override: bool = True,
    ) -> None:
        """Register multiple extension mappings.

        Args:
            formats: Mapping of extensions to media types.
            as_override: Whether the mappings count as user overrides that
                take precedence over content sniffing. Built-in default
                seeding passes False so defaults act only as name fallbacks.

        Raises:
            ValueError: If any extension or media type is invalid.
        """
        normalized_formats: dict[str, MediaType] = {}
        for extension, media_type in formats.items():
            normalized_extension = normalize_extension(extension)
            if not normalized_extension:
                raise ValueError(
                    "extension must contain at least one non-dot character"
                )
            normalized_formats[normalized_extension] = _coerce_media_type(media_type)

        with self._lock:
            self._extension_map.update(normalized_formats)
            if as_override:
                self._override_keys.update(normalized_formats)

    def lookup_override(self, extension: str) -> MediaType | None:
        """Look up an explicitly registered extension mapping.

        Unlike :meth:`lookup`, this never consults the MIME table or the
        built-in defaults; it only answers from user-supplied mappings
        (constructor ``formats``, config, or :meth:`register` calls), which
        are the only entries allowed to outrank content sniffing.

        Args:
            extension: File extension, with or without a leading dot. File names
                and paths are also accepted; only the final suffix is used.

        Returns:
            Registered media type, or ``None`` when the extension has no
            explicit registration.
        """
        normalized_extension = normalize_extension(extension)
        if not normalized_extension:
            return None

        with self._lock:
            if normalized_extension not in self._override_keys:
                return None
            return self._extension_map.get(normalized_extension)

    def lookup(self, extension: str) -> MediaType | None:
        """Look up a media type by extension.

        Args:
            extension: File extension, with or without a leading dot. File names
                and paths are also accepted; only the final suffix is used.

        Returns:
            Registered media type, the type winnow's extension table gives,
            the host MIME table's type when enabled, or ``None`` for unknown
            extensions.
        """
        return self.lookup_with_table(extension).media_type

    def lookup_with_table(self, extension: str) -> ExtensionLookup:
        """Look up a media type by extension and say which table answered.

        The registry's own formats answer first, then winnow's extension
        table (with ``include_defaults``), then the host's ``mimetypes``
        table (only with ``use_mime_fallback``).

        Args:
            extension: File extension, with or without a leading dot. File names
                and paths are also accepted; only the final suffix is used.

        Returns:
            The media type and the table that gave it.
        """
        normalized_extension = normalize_extension(extension)
        if not normalized_extension:
            return ExtensionLookup(media_type=None, table=NO_TABLE)

        with self._lock:
            media_type = self._extension_map.get(normalized_extension)
        if media_type is not None:
            return ExtensionLookup(media_type=media_type, table=REGISTRY_TABLE)
        if self._include_defaults:
            media_type = EXTENSION_MEDIA_TYPES.get(normalized_extension)
            if media_type is not None:
                return ExtensionLookup(
                    media_type=media_type,
                    table=EXTENSION_TABLE_NAME,
                )
        if self.use_mime_fallback:
            media_type = _lookup_mime_type(normalized_extension)
            if media_type is not None:
                return ExtensionLookup(media_type=media_type, table=HOST_MIME_TABLE)
        return ExtensionLookup(media_type=None, table=NO_TABLE)


def create_default_format_registry(
    *,
    use_mime_fallback: bool = DEFAULT_USE_MIME_FALLBACK,
) -> FormatRegistry:
    """Create a registry populated with Winnow's default media formats.

    Args:
        use_mime_fallback: Whether unknown extensions may fall back to the
            host's MIME table, whose answers differ between hosts.

    Returns:
        Registry containing built-in image, video, and audio formats.
    """
    return FormatRegistry(use_mime_fallback=use_mime_fallback)


def media_type_for_extension(
    extension: str,
    *,
    registry: FormatRegistry | None = None,
) -> MediaType | None:
    """Look up a media type using a registry.

    Args:
        extension: Extension, file name, or path to inspect.
        registry: Optional registry to use. Defaults to Winnow's shared default
            registry.

    Returns:
        Matching media type, or ``None`` when unknown.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    return active_registry.lookup(extension)


def detect_media_type(
    path: Path,
    *,
    registry: FormatRegistry | None = None,
) -> MediaType | None:
    """Detect a file's media type from overrides, content, then name.

    See :func:`winnow.media._detect.detect` for the rules and the evidence
    behind the answer.

    Args:
        path: File to classify. The file does not need to exist; detection then
            falls back to name-based inference.
        registry: Optional registry supplying overrides and the name-based
            fallback. Defaults to Winnow's shared default registry.

    Returns:
        Detected media type, or ``None`` when no rule establishes one.
    """
    return _detect.detect(path, registry=registry).media_type


def normalize_extension(extension: str) -> str:
    """Normalize an extension, file name, or path for registry lookup.

    A path with no suffix normalizes to ``""``. A bare dotless token (for
    example ``"makefile"``) is treated as an extension itself, so passing a
    suffix-less bare filename queries that token as a key.

    Args:
        extension: Raw extension, file name, or path.

    Returns:
        Lowercase extension without leading dots, or ``""`` when the input
        holds no extension.
    """
    candidate = extension.strip()
    if not candidate:
        return ""
    candidate = candidate.replace("\\", "/")
    has_separator = "/" in candidate
    if has_separator or "." in candidate:
        suffix = PurePath(candidate).suffix
        if not suffix and has_separator:
            return ""
        candidate = suffix or candidate
    return candidate.lstrip(".").casefold()


@functools.cache
def _host_mime_types() -> mimetypes.MimeTypes:
    """Build the private host MIME table the opt-in lookup asks.

    It holds CPython's built-in table, the host's MIME files
    (``mimetypes.knownfiles``) and, on Windows, the registry, then the RAW
    supplement, which also corrects misleading platform entries such as
    ``.dcr``. Built on first use, never at import, and never written to the
    process-wide ``mimetypes`` table.

    Returns:
        The table.
    """
    table = mimetypes.MimeTypes()
    for name in mimetypes.knownfiles:
        if Path(name).is_file():
            table.read(name)
    table.read_windows_registry()
    for extension, mime_type in RAW_IMAGE_MIME_TYPES.items():
        table.add_type(mime_type, f".{extension}")
    return table


def _lookup_mime_type(extension: str) -> MediaType | None:
    """Infer a media type from an extension using the host's MIME table.

    The host table reads platform files (for example ``/etc/mime.types`` or
    the Windows registry), so its answers vary across machines and Python
    versions; only registries built with ``use_mime_fallback=True`` ask it.

    Args:
        extension: Normalized extension without a leading dot.

    Returns:
        MIME-inferred media type, or ``None`` when MIME cannot identify an image,
        video, or audio extension.
    """
    mime_type, _ = _host_mime_types().guess_type(f"file.{extension}")
    if mime_type is None:
        return None
    mime_prefix, _, _ = mime_type.partition("/")
    return _MIME_PREFIX_TYPES.get(mime_prefix)


def _coerce_media_type(media_type: MediaType | str) -> MediaType:
    """Convert config media type values to ``MediaType`` instances.

    Args:
        media_type: Existing ``MediaType`` or string enum name/value.

    Returns:
        Normalized media type enum value.

    Raises:
        ValueError: If ``media_type`` does not identify a supported media type.
    """
    if isinstance(media_type, MediaType):
        return media_type

    normalized_value = media_type.casefold()
    try:
        return MediaType(normalized_value)
    except ValueError:
        raise ValueError(f"unsupported media type: {media_type}") from None


def _coerce_config_formats(value: object) -> dict[str, MediaType | str]:
    """Validate and return config-file extension mappings.

    Args:
        value: Raw ``formats`` value from the registry config section.

    Returns:
        Mapping of extension strings to media type values.

    Raises:
        ValueError: If ``value`` is not a valid extension mapping.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("formats must be a mapping")

    formats: dict[str, MediaType | str] = {}
    for extension, media_type in value.items():
        if not isinstance(extension, str):
            raise ValueError("format extension keys must be strings")
        if not isinstance(media_type, str):
            raise ValueError("format media types must be strings or MediaType values")
        formats[extension] = media_type
    return formats


def _coerce_config_bool(
    *,
    config: Mapping[str, object],
    key: str,
    default: bool,
) -> bool:
    """Read a boolean option from a config-file mapping.

    Args:
        config: Raw registry config section.
        key: Boolean option name.
        default: Value to use when ``key`` is absent.

    Returns:
        Configured boolean value or ``default``.

    Raises:
        ValueError: If the configured value is not a boolean.
    """
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be a boolean")


DEFAULT_FORMAT_REGISTRY = create_default_format_registry()
"""Shared mutable process-wide registry backing :func:`media_type_for_extension`.

Registrations on this instance are visible to every caller that relies on the
default; use :func:`create_default_format_registry` for an isolated registry."""

__all__ = [
    "DEFAULT_FORMAT_REGISTRY",
    "DEFAULT_FORMATS",
    "DEFAULT_USE_MIME_FALLBACK",
    "EXTENSION_ONLY_FORMATS",
    "HOST_MIME_TABLE",
    "NO_TABLE",
    "REGISTRY_TABLE",
    "ExtensionLookup",
    "FormatRegistry",
    "RAW_IMAGE_MIME_TYPES",
    "create_default_format_registry",
    "detect_media_type",
    "media_type_for_extension",
    "normalize_extension",
]
