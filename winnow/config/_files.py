"""Reading config files and their ``.local`` companion as Dynaconf did.

Until #391 winnow read its config file through Dynaconf, which gave the file
behaviour beyond plain YAML that users may rely on. This module keeps it:

- **Companion file.** ``<name>.local.<ext>`` next to the config file (for
  ``.winnow-config.yaml`` that is ``.winnow-config.local.yaml``) is read after
  it and overrides it. A config file whose name already contains ``.local.``
  has no companion. A missing companion is fine; an unreadable or invalid one
  is an error.
- **Merging.** Each top-level key of a later file, or a later key of the same
  file, is merged into what is there: mappings merge key by key (keys match
  case-insensitively), lists are concatenated (old items first) unless they
  are equal, and anything else, ``null`` included, replaces.
- **Flat keys.** A top-level key holding ``__`` or ``.`` is a path:
  ``routing.screenshots: Shots`` and ``routing__review: Rev`` set
  ``routing.screenshots`` and ``routing.review``. Only top-level keys are
  split, and surrounding spaces are trimmed from the first part only.
- **Markers.** String values starting with a cast marker are cast
  (:mod:`winnow.config._markers`).
- **Formats.** ``.toml``/``.tml`` files are TOML 1.0, ``.json`` is JSON, and
  every other file YAML 1.2. Dynaconf ignored files with other extensions
  without a word; winnow reads them as YAML.
- **Shape.** A file that nests collections more than :data:`MAX_DEPTH`
  deep, or holds a value that contains itself through a recursive YAML
  anchor, is refused with a ``ConfigError`` naming the file (#451).

Dynaconf features winnow does not keep raise ``ConfigError`` naming the key
instead of being ignored: ``dynaconf_include``, ``includes_for_dynaconf`` and
every other ``*_for_dynaconf`` key, file-level ``dynaconf_merge`` and
``dynaconf_dotted_lookup``, ``dynaconf_merge`` tokens inside values,
Dynaconf-only markers, and INI or Python config files.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum, auto
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.composer import MaxDepthExceededError

from winnow.config._markers import cast_markers_in
from winnow.config._shape import (
    MAX_DEPTH,
    NestingError,
    load_toml,
    nesting_depth,
    text_depth,
)
from winnow.exceptions import ConfigError

_LOCAL_INFIX: Final = ".local."
_TOML_SUFFIXES: Final = frozenset({".toml", ".tml"})
_JSON_SUFFIXES: Final = frozenset({".json"})
_UNSUPPORTED_SUFFIXES: Final[dict[str, str]] = {
    ".ini": "INI",
    ".conf": "INI",
    ".properties": "INI",
    ".py": "Python",
}
"""Formats Dynaconf accepted that winnow rejects instead of reading."""

_FILE_LEVEL_KEYS: Final = frozenset(
    {"dynaconf_include", "dynaconf_merge", "dynaconf_merge_unique"}
    | {"dynaconf_dotted_lookup"},
)
"""Top-level keys that changed how Dynaconf loaded the file."""
_MERGE_TOKENS: Final = frozenset({"dynaconf_merge", "dynaconf_merge_unique"})
"""Keys or list items that changed how Dynaconf merged a value."""
_FOR_DYNACONF_SUFFIX: Final = "_for_dynaconf"


class ConfigFormat(StrEnum):
    """The format a config file is read in, chosen by its suffix."""

    YAML = auto()
    TOML = auto()
    JSON = auto()


def config_format(path: Path) -> ConfigFormat:
    """Return the format a config file is read in.

    Args:
        path: The config file; it need not exist.

    Returns:
        TOML for ``.toml``/``.tml``, JSON for ``.json`` and YAML otherwise,
        whatever the suffix's letter case.

    Raises:
        ConfigError: If the suffix names a format winnow does not read.
    """
    suffix = path.suffix.lower()
    unsupported = _UNSUPPORTED_SUFFIXES.get(suffix)
    if unsupported is not None:
        raise ConfigError(
            f"{unsupported} config files are not supported; use YAML",
            operation="load_config",
            file_path=path,
        )
    if suffix in _TOML_SUFFIXES:
        return ConfigFormat.TOML
    if suffix in _JSON_SUFFIXES:
        return ConfigFormat.JSON
    return ConfigFormat.YAML


def companion_path(config_path: Path) -> Path | None:
    """Return the ``.local`` companion of a config file, as Dynaconf named it.

    Args:
        config_path: The config file.

    Returns:
        ``<name>.local.<ext>`` in the same directory, or ``None`` when the
        file name has no extension or already contains ``.local.``.
    """
    name = config_path.name
    if _LOCAL_INFIX in name:
        return None
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        return None
    return config_path.with_name(f"{stem}{_LOCAL_INFIX}{extension}")


def read_config_layers(
    config_path: Path,
    *,
    main_document: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Read a config file and its ``.local`` companion into one mapping.

    Args:
        config_path: The config file.
        main_document: Parsed content to read in place of
            ``config_path``'s, which then need not exist; used to check a
            change before writing it without parsing the content again.

    Returns:
        The merged settings, all mapping keys lowercase.

    Raises:
        ConfigError: If a file uses a Dynaconf feature winnow does not keep.
        OSError: If a file cannot be read.
        ValueError: If a file is not valid in its format, or its top level is
            not a mapping.
    """
    store: dict[str, object] = {}
    companion = companion_path(config_path)
    for path in (config_path, companion):
        if path is None or (path is companion and not path.is_file()):
            continue
        document = (
            main_document
            if path is config_path and main_document is not None
            else _read_document(path)
        )
        _apply_file(store, document, path=path)
    return store


def settings_of(
    document: Mapping[str, object],
    path: Path,
    *,
    cast: bool = True,
) -> dict[str, object]:
    """Read one parsed config file's settings, without its companion.

    Args:
        document: The file's top-level mapping, from :func:`parse_document`.
        path: The file it belongs to.
        cast: Whether to apply cast markers.

    Returns:
        The file's settings, read as :func:`read_config_layers` reads them.

    Raises:
        ConfigError: If the content uses a Dynaconf feature winnow does not
            keep.
        ValueError: If a value uses a cast marker winnow does not support.
    """
    store: dict[str, object] = {}
    _apply_file(store, document, path=path, cast=cast)
    return store


def read_value(
    value: object,
    *,
    cast: bool = True,
    parts: Sequence[str] = (),
    path: Path | None = None,
) -> object:
    """Return a value as reading it from a config file gives it.

    This is where a read value is measured: after cast markers are applied
    and a flat key's parts are nested, before any recursive function walks
    it. The mapping the value sits in counts as one level, as it does in
    :func:`parse_document`.

    Args:
        value: A value as written in a file, which
            :func:`parse_document` has measured.
        cast: Whether to apply cast markers.
        parts: The parts of a flat key after its first, which nest the
            value one mapping each.
        path: The file the value is read from, for the error.

    Returns:
        The value, nested in ``parts``, with mapping keys lowercased and,
        when ``cast`` is set, cast markers applied.

    Raises:
        ConfigError: If the value read nests more than :data:`MAX_DEPTH`
            deep with the mapping it sits in.
        ValueError: If the value uses a cast marker winnow does not support.
    """
    read = cast_markers_in(value) if cast else value
    for part in reversed(parts):
        read = {part.lower(): read}
    depth = nesting_depth(read)
    if depth is None or depth >= MAX_DEPTH:
        raise _shape_error(path, too_deep=depth is not None)
    return _lowercase_keys(read)


def _read_document(path: Path) -> Mapping[str, object]:
    """Parse one config file in the format its extension names.

    Args:
        path: The file.

    Returns:
        Its top-level mapping; an empty document reads as an empty mapping.

    Raises:
        ConfigError: If the extension names a format winnow does not read.
        ValueError: If the content is invalid or not a mapping.
    """
    if config_format(path) is ConfigFormat.TOML:
        return parse_document(path.read_bytes().decode("utf-8"), path)
    return parse_document(path.read_text(encoding="utf-8"), path)


def parse_document(text: str, path: Path) -> Mapping[str, object]:
    """Parse config file content in the format ``path``'s extension names.

    Args:
        text: The content.
        path: The file it belongs to.

    Returns:
        Its top-level mapping; an empty document reads as an empty mapping.

    Raises:
        ConfigError: If the extension names a format winnow does not read,
            or the content nests too deeply or holds a value that contains
            itself.
        ValueError: If the content is invalid or not a mapping.
    """
    file_format = config_format(path)
    document: object
    if file_format is ConfigFormat.TOML:
        document = _load_toml(text, path)
    elif file_format is ConfigFormat.JSON:
        if text_depth(text) > MAX_DEPTH:
            raise _shape_error(path, too_deep=True)
        document = json.loads(text)
    else:
        stream = io.StringIO(text)
        stream.name = str(path)
        yaml = YAML(typ="safe", pure=True)
        yaml.max_depth = MAX_DEPTH + 1
        try:
            document = yaml.load(stream)
        except MaxDepthExceededError as exc:
            raise _shape_error(path, too_deep=True) from exc
    depth = nesting_depth(document)
    if depth is None or depth > MAX_DEPTH:
        raise _shape_error(path, too_deep=depth is not None)
    if document is None:
        return {}
    if not isinstance(document, Mapping):
        message = (
            f"the top level of {path} must be a mapping, "
            f"not {type(document).__name__}"
        )
        raise ValueError(message)
    return document


def _load_toml(text: str, path: Path) -> object:
    """Parse TOML content, refusing what tomli refuses as too deep.

    Raises:
        ConfigError: If tomli refuses the content as nested too deeply.
        ValueError: If the content is not valid TOML.
    """
    try:
        return load_toml(text)
    except NestingError as exc:
        raise _shape_error(path, too_deep=True) from exc


def _shape_error(path: Path | None, *, too_deep: bool) -> ConfigError:
    """Build the error for a file nested too deeply or holding a cycle."""
    reason = (
        f"it nests values more than {MAX_DEPTH} levels deep"
        if too_deep
        else "it holds a value that contains itself, such as a recursive " "YAML anchor"
    )
    return ConfigError(
        f"Unable to load Winnow configuration: {reason}",
        operation="load_config",
        file_path=path,
    )


def _apply_file(
    store: dict[str, object],
    document: Mapping[str, object],
    *,
    path: Path,
    cast: bool = True,
) -> None:
    """Merge one file's settings into ``store``, in the file's key order.

    As in Dynaconf, top-level keys that differ only in case (after the part
    before any ``__``) collapse to the last one before anything is merged.

    Args:
        store: Settings read so far; updated in place.
        document: The file's top-level mapping.
        path: The file, for error messages.
        cast: Whether to apply cast markers.
    """
    collapsed: dict[str, tuple[str, object]] = {}
    for raw_key, value in document.items():
        key = str(raw_key)
        _reject_dynaconf_key(key, path=path)
        _reject_merge_tokens(value, path=path, where=key)
        collapsed[_upperfy(key)] = (key, value)
    for key, value in collapsed.values():
        parts = key.replace("__", ".").split(".")
        top = parts[0].strip().lower()
        nested = read_value(value, cast=cast, parts=parts[1:], path=path)
        store[top] = merge_values(store.get(top), nested)


def _upperfy(key: str) -> str:
    """Upper-case a key's first ``__`` part, as Dynaconf keyed a file's data."""
    head, dunder, rest = key.partition("__")
    return head.upper() + dunder + rest


def merge_values(old: object, new: object) -> object:
    """Merge a later value into an earlier one with Dynaconf's rules.

    Args:
        old: The value already set, or ``None``.
        new: The value being set.

    Returns:
        ``new`` when either is ``None`` or they are equal; ``old + new`` for
        two different lists; for two mappings, ``old``'s keys in order, merged
        with ``new``'s where both have them, then ``new``'s other keys;
        otherwise ``new``.
    """
    if old is None or new is None or old == new:
        return new
    if isinstance(old, list) and isinstance(new, list):
        return [*old, *new]
    if isinstance(old, dict) and isinstance(new, dict):
        merged = {
            key: merge_values(value, new[key]) if key in new else value
            for key, value in old.items()
        }
        merged.update({key: value for key, value in new.items() if key not in old})
        return merged
    return new


def _lowercase_keys(value: object) -> object:
    """Lowercase every mapping key in a value; later duplicates win."""
    if isinstance(value, Mapping):
        return {str(key).lower(): _lowercase_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_lowercase_keys(item) for item in value]
    return value


def _reject_dynaconf_key(key: str, *, path: Path) -> None:
    """Raise if a top-level key is a Dynaconf loading setting.

    Raises:
        ConfigError: For ``dynaconf_include``, ``dynaconf_merge``,
            ``dynaconf_merge_unique``, ``dynaconf_dotted_lookup`` and any
            ``*_for_dynaconf`` key.
    """
    lowered = key.strip().lower()
    if lowered in _FILE_LEVEL_KEYS or lowered.endswith(_FOR_DYNACONF_SUFFIX):
        raise ConfigError(
            f"the Dynaconf setting {key!r} is not supported in a winnow " "config file",
            operation="load_config",
            file_path=path,
        )


def _reject_merge_tokens(value: object, *, path: Path, where: str) -> None:
    """Raise if a value holds a Dynaconf merge token at any depth.

    Raises:
        ConfigError: For a ``dynaconf_merge`` or ``dynaconf_merge_unique`` key
            in a mapping or item in a list.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().lower() in _MERGE_TOKENS:
                raise _merge_token_error(str(key), path=path, where=where)
            _reject_merge_tokens(item, path=path, where=f"{where}.{key}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item in _MERGE_TOKENS:
                raise _merge_token_error(item, path=path, where=where)
            _reject_merge_tokens(item, path=path, where=where)


def _merge_token_error(token: str, *, path: Path, where: str) -> ConfigError:
    """Build the error for a Dynaconf merge token."""
    return ConfigError(
        f"the Dynaconf merge token {token!r} in {where!r} is not supported in a "
        "winnow config file",
        operation="load_config",
        file_path=path,
    )
