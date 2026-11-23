"""Changing one key of a config file in the form the user wrote it.

``config set`` reads the target file as written, changes the one key and
writes the file back in its own format, so the companion file, the
environment, cast markers and the file's other keys never leak into it
(#429). YAML files are edited in place by :mod:`winnow.config._yaml_splice`;
JSON files keep their keys and values but are re-serialised and
re-indented; a new file is written as ``config set`` always wrote new files.
TOML files are refused, as winnow has no TOML writer. This module holds the
format handling and the key-matching rules both editors share.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from winnow.config._files import ConfigFormat, config_format
from winnow.config._markers import cast_marker
from winnow.config._shape import children
from winnow.config.schema import render_config_yaml
from winnow.exceptions import ConfigError

_DEFAULT_JSON_INDENT: Final = 2
_CRLF: Final = "\r\n"
_BOM: Final = "﻿"


def writable_format(path: Path) -> ConfigFormat:
    """Return the format ``config set`` writes a config file in.

    Args:
        path: The config file; it need not exist.

    Returns:
        The file's own format, YAML or JSON.

    Raises:
        ConfigError: For a TOML file, which winnow cannot write without losing
            its content, or a format winnow does not read.
    """
    file_format = config_format(path)
    if file_format is ConfigFormat.TOML:
        raise ConfigError(
            "config set cannot write TOML config files; edit the file by hand "
            "or use a YAML config file",
            operation="set_config",
            file_path=path,
        )
    return file_format


def load_json(text: str | None) -> MutableMapping[str, object]:
    """Parse JSON config file content for editing.

    Args:
        text: The file's content, or ``None`` for a file that does not exist.

    Returns:
        The top-level object; an empty mapping for a missing file or
        ``null``. An empty file is invalid JSON, as the reader says.

    Raises:
        ValueError: If the content is invalid, an object repeats a key, or
            the top level is not an object.
    """
    if text is None:
        return {}
    document = json.loads(text, object_pairs_hook=_unique_keys)
    if document is None:
        return {}
    if not isinstance(document, MutableMapping):
        message = f"the top level must be a mapping, not {type(document).__name__}"
        raise ValueError(message)
    return document


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a key it repeats.

    Raises:
        ValueError: If a key appears twice.
    """
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            message = f"duplicate key {key!r} in a JSON object"
            raise ValueError(message)
        document[key] = value
    return document


@dataclass(frozen=True, slots=True)
class TextLayout:
    """How a config file lays out its text around the content."""

    crlf: bool = False
    """Whether lines end with CRLF rather than LF."""
    bom: bool = False
    """Whether the file starts with a UTF-8 byte order mark."""

    def apply(self, text: str) -> str:
        """Return ``\\n``-ended content laid out as the file was."""
        if self.crlf:
            text = text.replace("\n", _CRLF)
        return f"{_BOM}{text}" if self.bom else text


def split_layout(raw: str, *, path: Path) -> tuple[str, TextLayout]:
    """Return file content with ``\\n`` line ends and no BOM, and its layout.

    Args:
        raw: The file's text, line ends untranslated.
        path: The file, for the error.

    Returns:
        The content and how the file lays it out.

    Raises:
        ConfigError: If the file mixes CRLF and LF line ends or holds a lone
            CR, as the file could not be written back with the same ends.
    """
    bom = raw.startswith(_BOM)
    text = raw.removeprefix(_BOM)
    crlf = _CRLF in text
    rest = text.replace(_CRLF, "")
    if "\r" in rest or (crlf and "\n" in rest):
        raise ConfigError(
            "config set cannot keep this file's line endings: it mixes CRLF "
            "and LF or holds a lone CR; use one kind of line ending or edit "
            "the file by hand",
            operation="set_config",
            file_path=path,
        )
    return text.replace(_CRLF, "\n"), TextLayout(crlf=crlf, bom=bom)


def dump_document(
    document: Mapping[str, object],
    file_format: ConfigFormat,
    *,
    original: str | None,
) -> str:
    """Render a whole document: a JSON file, or a new YAML file.

    Args:
        document: The edited top-level mapping.
        file_format: The file's format.
        original: A JSON file's content before the edit with ``\\n`` line
            ends and no BOM, whose indent the new content keeps; ``None``
            for a new file.

    Returns:
        The new content, ending in the newlines the original ended with,
        or in one newline for a new or empty file.

    Raises:
        ValueError: If JSON cannot hold a value.
        TypeError: If JSON cannot serialize a value.
    """
    if file_format is not ConfigFormat.JSON:
        return render_config_yaml(document)
    indent = _DEFAULT_JSON_INDENT if original is None else _guess_json_indent(original)
    text = json.dumps(document, indent=indent, ensure_ascii=False, allow_nan=False)
    if not original:
        return f"{text}\n"
    return text + original[len(original.rstrip("\n")) :]


def check_finite(value: object, *, key: str) -> None:
    """Refuse NaN and infinite numbers anywhere in a value.

    Neither JSON nor the config model can hold them, and YAML reads them
    back as a different value. The value is walked without recursing, into
    what :func:`~winnow.config._shape.children` calls a collection, mapping
    keys included.

    Raises:
        ConfigError: If ``value`` holds a float that is not finite.
    """
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ConfigError(
                f"{item} is not a finite number; config values must be finite",
                operation="set_config",
                details={"key": key},
            )
        items = children(item)
        if items is not None and id(item) not in seen:
            seen.add(id(item))
            pending.extend(items)


def set_in_document(
    document: MutableMapping[str, object],
    key_parts: list[str],
    value: object,
) -> None:
    """Set a dotted key in a JSON or new config document, where the file sets it.

    The reader lets a file set a nested setting through a nested mapping or a
    flat top-level ``a.b``/``a__b`` key, with later entries winning, and
    matches keys case-insensitively. The value goes to the last top-level
    entry that covers the key, so it is the one that takes effect, or to a
    new entry when none does.

    Args:
        document: The file's top-level mapping; changed in place.
        key_parts: The key's non-empty dot-separated parts.
        value: The value to write.

    Raises:
        ConfigError: If the key passes through a value that is not a mapping
            or a cast string, or has a child another top-level flat key sets.
    """
    refuse_flat_children([str(key) for key in document], key_parts)
    wanted = [part.lower() for part in key_parts]
    for raw_key in reversed(list(document)):
        path = entry_path(str(raw_key))
        if path != wanted[: len(path)]:
            continue
        rest = key_parts[len(path) :]
        if not rest or document[raw_key] is None:
            document[raw_key] = nest(rest, value)
            return
        child = _child_mapping(document, raw_key, key_parts)
        _set_nested(child, rest, value, key_parts)
        return
    _set_nested(document, key_parts, value, key_parts)


def _set_nested(
    mapping: MutableMapping[str, object],
    parts: list[str],
    value: object,
    key_parts: list[str] | None = None,
) -> None:
    """Set ``parts`` inside a nested mapping, matching keys case-insensitively."""
    whole = key_parts or parts
    current = mapping
    for index, part in enumerate(parts[:-1]):
        slot = matching_key(list(current), part)
        if slot is None or current[slot] is None:
            current[slot or part] = nest(parts[index + 1 :], value)
            return
        current = _child_mapping(current, slot, whole)
    current[matching_key(list(current), parts[-1]) or parts[-1]] = value


def _child_mapping(
    mapping: MutableMapping[str, object],
    key: str,
    key_parts: list[str],
) -> MutableMapping[str, object]:
    """Return the mapping under ``key``; a ``null`` there is replaced by the caller.

    Raises:
        ConfigError: If the value under ``key`` is a cast string or not a
            mapping.
    """
    child = mapping[key]
    if isinstance(child, str):
        refuse_cast_string(child, key, key_parts)
    if not isinstance(child, MutableMapping):
        raise traverse_error(key, key_parts)
    return child


def refuse_flat_children(keys: list[str], key_parts: list[str]) -> None:
    """Refuse a key when a top-level flat key sets something below it.

    The reader merges that flat key into whatever value is written, so the
    key would not end up holding the value given.

    Args:
        keys: The file's top-level keys, as written.
        key_parts: The key being set.

    Raises:
        ConfigError: Naming the flat key.
    """
    wanted = [part.lower() for part in key_parts]
    for raw_key in keys:
        path = entry_path(raw_key)
        if len(path) > len(wanted) and path[: len(wanted)] == wanted:
            raise ConfigError(
                f"config set cannot replace {'.'.join(key_parts)}: the file also "
                f"sets {raw_key} as a separate flat key, which would be merged "
                "into the new value; set that key on its own or edit the file "
                "by hand",
                operation="set_config",
                details={"key": ".".join(key_parts), "flat_key": raw_key},
            )


def refuse_cast_string(text: str, key: str, key_parts: list[str]) -> None:
    """Refuse to set a key below a value written as a cast string.

    Raises:
        ConfigError: If ``text`` starts with a cast marker.
    """
    if cast_marker(text)[0]:
        dotted = ".".join(key_parts)
        raise ConfigError(
            f"config set cannot change {dotted}: {key} is written as a cast "
            f"string ({text.partition(' ')[0]}); set the whole {key} or edit "
            "the file by hand",
            operation="set_config",
            details={"key": dotted, "segment": key},
        )


def traverse_error(key: str, key_parts: list[str]) -> ConfigError:
    """Return the error for a key below a value that is not a mapping."""
    return ConfigError(
        "Configuration key traverses a non-object value",
        operation="set_config",
        details={"key": ".".join(key_parts), "segment": key},
    )


def nest(parts: list[str], value: object) -> object:
    """Return ``value`` nested in one mapping per part, outermost first."""
    for part in reversed(parts):
        value = {part: value}
    return value


def matching_key(keys: list[str], part: str) -> str | None:
    """Return the last of ``keys`` equal to ``part`` ignoring case, if any."""
    existing = [key for key in keys if str(key).lower() == part.lower()]
    return existing[-1] if existing else None


def entry_path(key: str) -> list[str]:
    """Return the lowercase setting path a top-level key sets, as the reader does."""
    parts = key.replace("__", ".").split(".")
    return [parts[0].strip().lower(), *(part.lower() for part in parts[1:])]


def _guess_json_indent(text: str) -> str | None:
    """Return the indent of the first indented line, or ``None`` for one line."""
    for line in text.splitlines()[1:]:
        content = line.lstrip(" \t")
        if content and len(content) < len(line):
            return line[: len(line) - len(content)]
    return None
