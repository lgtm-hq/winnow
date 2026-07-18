"""Extensible media format registry and layered media-type detection.

Media types are resolved through three layers, most authoritative first:

1. User overrides registered on a :class:`FormatRegistry`.
2. Content sniffing of magic bytes via ``puremagic``.
3. Name-based inference through the standard library ``mimetypes`` table,
   seeded once at import with RAW camera formats the table lacks.

The registry itself only hand-maintains the RAW supplement; every mainstream
extension is answered by the MIME table or by sniffing file content.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from pathlib import Path, PurePath
from threading import Lock
from types import MappingProxyType
from typing import Self

import puremagic

from winnow.models.media import MediaType

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
"""RAW camera extensions missing from the stdlib ``mimetypes`` table."""

DEFAULT_FORMATS: Mapping[str, MediaType] = MappingProxyType(
    {extension: MediaType.IMAGE for extension in RAW_IMAGE_MIME_TYPES},
)
"""RAW-format supplement seeded into default registries.

Mainstream formats (JPEG, PNG, MP4, MP3, ...) are intentionally absent; they
resolve through content sniffing or the stdlib ``mimetypes`` table instead of a
hand-maintained list.
"""


def _seed_raw_mime_types() -> None:
    """Register RAW image MIME types with the stdlib ``mimetypes`` table.

    Runs once at import so ``mimetypes.guess_type`` recognizes RAW camera
    extensions (and corrects misleading platform entries such as ``.dcr``)
    process-wide.
    """
    for extension, mime_type in RAW_IMAGE_MIME_TYPES.items():
        mimetypes.add_type(mime_type, f".{extension}")


_seed_raw_mime_types()

_MIME_PREFIX_TYPES: Mapping[str, MediaType] = MappingProxyType(
    {
        "audio": MediaType.AUDIO,
        "image": MediaType.IMAGE,
        "video": MediaType.VIDEO,
    },
)


class FormatRegistry:
    """Map media file extensions to supported media types.

    Args:
        formats: Optional custom extension mapping. Values may be ``MediaType``
            instances or media type strings such as ``"image"``.
        include_defaults: Whether to seed the registry with Winnow's built-in
            common media formats before applying custom formats.
        use_mime_fallback: Whether ``lookup`` should ask ``mimetypes`` when an
            extension is not registered.
    """

    __slots__ = ("_extension_map", "_lock", "use_mime_fallback")

    def __init__(
        self,
        formats: Mapping[str, MediaType | str] | None = None,
        *,
        include_defaults: bool = True,
        use_mime_fallback: bool = True,
    ) -> None:
        self._extension_map: dict[str, MediaType] = {}
        self._lock = Lock()
        self.use_mime_fallback = use_mime_fallback

        if include_defaults:
            self.register_many(DEFAULT_FORMATS)
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
            default=True,
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

    def register_many(
        self,
        formats: Mapping[str, MediaType | str],
    ) -> None:
        """Register multiple extension mappings.

        Args:
            formats: Mapping of extensions to media types.

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

    def lookup_override(self, extension: str) -> MediaType | None:
        """Look up an explicitly registered extension mapping.

        Unlike :meth:`lookup`, this never consults the MIME table; it only
        answers from mappings registered on this instance (defaults, config,
        or :meth:`register` calls).

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
            return self._extension_map.get(normalized_extension)

    def lookup(self, extension: str) -> MediaType | None:
        """Look up a media type by extension.

        Args:
            extension: File extension, with or without a leading dot. File names
                and paths are also accepted; only the final suffix is used.

        Returns:
            Registered media type, MIME-inferred media type when enabled, or
            ``None`` for unknown extensions.
        """
        normalized_extension = normalize_extension(extension)
        if not normalized_extension:
            return None

        media_type = self.lookup_override(normalized_extension)
        if media_type is not None:
            return media_type
        if not self.use_mime_fallback:
            return None
        return _lookup_mime_type(normalized_extension)


def create_default_format_registry(
    *,
    use_mime_fallback: bool = True,
) -> FormatRegistry:
    """Create a registry populated with Winnow's default media formats.

    Args:
        use_mime_fallback: Whether unknown extensions may fall back to MIME type
            detection.

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
        Matching media type, MIME-inferred media type, or ``None`` when unknown.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    return active_registry.lookup(extension)


def detect_media_type(
    path: Path,
    *,
    registry: FormatRegistry | None = None,
) -> MediaType | None:
    """Detect a file's media type using layered detection.

    Layers are consulted most-authoritative first:

    1. Explicit registry overrides for the file's extension.
    2. Magic-byte content sniffing via ``puremagic``.
    3. Name-based inference through the ``mimetypes`` table, which covers
       unreadable or absent files.

    Args:
        path: File to classify. The file does not need to exist; detection then
            falls back to name-based inference.
        registry: Optional registry supplying overrides and the name-based
            fallback. Defaults to Winnow's shared default registry.

    Returns:
        Detected media type, or ``None`` when no layer can classify the file.
    """
    active_registry = registry if registry is not None else DEFAULT_FORMAT_REGISTRY
    override = active_registry.lookup_override(path.suffix)
    if override is not None:
        return override

    sniffed = _sniff_media_type(path)
    if sniffed is not None:
        return sniffed

    return active_registry.lookup(path.suffix)


def _sniff_media_type(path: Path) -> MediaType | None:
    """Sniff a file's media type from its magic bytes.

    Args:
        path: File whose content should be inspected.

    Returns:
        Media type of the strongest image, video, or audio signature match, or
        ``None`` when the file is unreadable or its content is unrecognized.
    """
    try:
        matches = puremagic.magic_file(str(path))
    except (puremagic.PureError, OSError, ValueError):
        return None

    for match in matches:
        mime_type = match.mime_type
        if not mime_type:
            continue
        mime_prefix, _, _ = mime_type.partition("/")
        media_type = _MIME_PREFIX_TYPES.get(mime_prefix)
        if media_type is not None:
            return media_type
    return None


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


def _lookup_mime_type(extension: str) -> MediaType | None:
    """Infer a media type from an extension using the stdlib MIME table.

    ``mimetypes`` consults platform tables (for example ``/etc/mime.types`` or
    the Windows registry), so fallback results can vary across machines.

    Args:
        extension: Normalized extension without a leading dot.

    Returns:
        MIME-inferred media type, or ``None`` when MIME cannot identify an image,
        video, or audio extension.
    """
    mime_type, _ = mimetypes.guess_type(f"file.{extension}")
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
    "FormatRegistry",
    "RAW_IMAGE_MIME_TYPES",
    "create_default_format_registry",
    "detect_media_type",
    "media_type_for_extension",
    "normalize_extension",
]
