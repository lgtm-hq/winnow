"""Extensible media format registry.

The registry maps file extensions to :class:`winnow.models.media.MediaType`.
Unknown extensions return ``None`` unless the optional MIME fallback can infer an
image, video, or audio type from the standard library ``mimetypes`` table.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from pathlib import PurePath
from threading import Lock
from types import MappingProxyType
from typing import Self

from winnow.models.media import MediaType

IMAGE_FORMATS: Mapping[str, MediaType] = MappingProxyType(
    {
        "3fr": MediaType.IMAGE,
        "ari": MediaType.IMAGE,
        "arw": MediaType.IMAGE,
        "bmp": MediaType.IMAGE,
        "cr2": MediaType.IMAGE,
        "cr3": MediaType.IMAGE,
        "dcr": MediaType.IMAGE,
        "dng": MediaType.IMAGE,
        "erf": MediaType.IMAGE,
        "gif": MediaType.IMAGE,
        "heic": MediaType.IMAGE,
        "heif": MediaType.IMAGE,
        "jpeg": MediaType.IMAGE,
        "jpg": MediaType.IMAGE,
        "k25": MediaType.IMAGE,
        "kdc": MediaType.IMAGE,
        "mrw": MediaType.IMAGE,
        "nef": MediaType.IMAGE,
        "nrw": MediaType.IMAGE,
        "orf": MediaType.IMAGE,
        "pef": MediaType.IMAGE,
        "png": MediaType.IMAGE,
        "raf": MediaType.IMAGE,
        "raw": MediaType.IMAGE,
        "rw2": MediaType.IMAGE,
        "rwl": MediaType.IMAGE,
        "sr2": MediaType.IMAGE,
        "srf": MediaType.IMAGE,
        "srw": MediaType.IMAGE,
        "tif": MediaType.IMAGE,
        "tiff": MediaType.IMAGE,
        "webp": MediaType.IMAGE,
        "x3f": MediaType.IMAGE,
    },
)
VIDEO_FORMATS: Mapping[str, MediaType] = MappingProxyType(
    {
        "avi": MediaType.VIDEO,
        "mkv": MediaType.VIDEO,
        "mov": MediaType.VIDEO,
        "mp4": MediaType.VIDEO,
        "webm": MediaType.VIDEO,
    },
)
AUDIO_FORMATS: Mapping[str, MediaType] = MappingProxyType(
    {
        "aac": MediaType.AUDIO,
        "flac": MediaType.AUDIO,
        "mp3": MediaType.AUDIO,
        "ogg": MediaType.AUDIO,
        "wav": MediaType.AUDIO,
    },
)
DEFAULT_FORMATS: Mapping[str, MediaType] = MappingProxyType(
    {
        **IMAGE_FORMATS,
        **VIDEO_FORMATS,
        **AUDIO_FORMATS,
    },
)

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

        with self._lock:
            media_type = self._extension_map.get(normalized_extension)
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


def normalize_extension(extension: str) -> str:
    """Normalize an extension, file name, or path for registry lookup.

    Args:
        extension: Raw extension, file name, or path.

    Returns:
        Lowercase extension without leading dots.
    """
    candidate = extension.strip()
    if not candidate:
        return ""
    if "." in candidate or "/" in candidate or "\\" in candidate:
        suffix = PurePath(candidate).suffix
        candidate = suffix or candidate
    return candidate.lstrip(".").casefold()


def _lookup_mime_type(extension: str) -> MediaType | None:
    """Infer a media type from an extension using the stdlib MIME table.

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

__all__ = [
    "AUDIO_FORMATS",
    "DEFAULT_FORMATS",
    "DEFAULT_FORMAT_REGISTRY",
    "FormatRegistry",
    "IMAGE_FORMATS",
    "VIDEO_FORMATS",
    "create_default_format_registry",
    "media_type_for_extension",
    "normalize_extension",
]
