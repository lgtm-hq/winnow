"""Changing one key of a config file in the form the user wrote it.

``config set`` reads the target file as written, changes the one key and
writes the file back in its own format, so the companion file, the
environment, cast markers and the file's other keys never leak into it
(#429). YAML files keep their comments, quoting and key order as far as
``ruamel.yaml``'s round trip allows; JSON files keep their keys and values
but are re-indented. TOML files are refused, as winnow has no TOML writer.
"""

from __future__ import annotations

import io
import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.util import load_yaml_guess_indent

from winnow.config._files import ConfigFormat, config_format
from winnow.config.schema import render_config_yaml
from winnow.exceptions import ConfigError

_DEFAULT_MAPPING_INDENT: Final = 2
_DEFAULT_SEQUENCE_INDENT: Final = 4
_DEFAULT_SEQUENCE_OFFSET: Final = 2
_DEFAULT_JSON_INDENT: Final = 2
_YAML_WIDTH: Final = 4096
"""Wide enough that the dump never folds a long value onto a new line."""


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


def load_document(
    text: str | None,
    file_format: ConfigFormat,
) -> MutableMapping[str, object]:
    """Parse config file content for editing.

    Args:
        text: The file's content, or ``None`` for a file that does not exist.
        file_format: The file's format, YAML or JSON.

    Returns:
        The top-level mapping, round-trip loaded for YAML; an empty mapping
        for a missing or empty file.

    Raises:
        ValueError: If the content is invalid or its top level is not a
            mapping.
    """
    if text is None:
        return {}
    document: object
    if file_format is ConfigFormat.JSON:
        document = json.loads(text) if text.strip() else None
    else:
        document = _round_trip_yaml(text).load(text)
    if document is None:
        return CommentedMap() if file_format is ConfigFormat.YAML else {}
    if not isinstance(document, MutableMapping):
        message = f"the top level must be a mapping, not {type(document).__name__}"
        raise ValueError(message)
    return document


def dump_document(
    document: MutableMapping[str, object],
    file_format: ConfigFormat,
    *,
    original: str | None,
) -> str:
    """Render an edited document in its file's format.

    Args:
        document: The edited top-level mapping.
        file_format: The file's format, YAML or JSON.
        original: The file's content before the edit, or ``None`` for a new
            file, which is written as ``config set`` always wrote new files.

    Returns:
        The new file content.
    """
    if file_format is ConfigFormat.JSON:
        indent = (
            _DEFAULT_JSON_INDENT if original is None else _guess_json_indent(original)
        )
        text = json.dumps(document, indent=indent, ensure_ascii=False, allow_nan=False)
        keeps_newline = original is None or original.endswith("\n")
        return f"{text}\n" if keeps_newline else text
    if original is None:
        return render_config_yaml(document)
    stream = io.StringIO()
    _round_trip_yaml(original).dump(document, stream)
    return stream.getvalue()


def set_in_document(
    document: MutableMapping[str, object],
    key_parts: list[str],
    value: object,
) -> None:
    """Set a dotted key in a config document, where the file sets it.

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
        ConfigError: If the key passes through a value that is not a mapping.
    """
    wanted = [part.lower() for part in key_parts]
    for raw_key in reversed(list(document)):
        entry_path = _entry_path(str(raw_key))
        if entry_path != wanted[: len(entry_path)]:
            continue
        if len(entry_path) == len(wanted):
            document[raw_key] = value
            return
        child = _child_mapping(document, raw_key, key_parts)
        _set_nested(child, key_parts[len(entry_path) :], value, key_parts)
        return
    _set_nested(document, key_parts, value, key_parts)


def _set_nested(
    mapping: MutableMapping[str, object],
    parts: list[str],
    value: object,
    key_parts: list[str],
) -> None:
    """Set ``parts`` inside a nested mapping, matching keys case-insensitively.

    Args:
        mapping: The mapping to set the key in; changed in place.
        parts: The key's remaining parts.
        value: The value to write.
        key_parts: The whole key, for error messages.
    """
    current = mapping
    for part in parts[:-1]:
        slot = _matching_key(current, part)
        if slot not in current:
            current[slot] = {}
        current = _child_mapping(current, slot, key_parts)
    current[_matching_key(current, parts[-1])] = value


def _matching_key(mapping: MutableMapping[str, object], part: str) -> str:
    """Return the last key of ``mapping`` equal to ``part`` ignoring case, or it."""
    existing = [key for key in mapping if str(key).lower() == part.lower()]
    return existing[-1] if existing else part


def _child_mapping(
    mapping: MutableMapping[str, object],
    key: str,
    key_parts: list[str],
) -> MutableMapping[str, object]:
    """Return the mapping under ``key``, replacing a ``null`` with a new one.

    Raises:
        ConfigError: If the value under ``key`` is neither a mapping nor null.
    """
    child = mapping[key]
    if child is None:
        child = mapping[key] = {}
    if not isinstance(child, MutableMapping):
        raise ConfigError(
            "Configuration key traverses a non-object value",
            operation="set_config",
            details={"key": ".".join(key_parts), "segment": key},
        )
    return child


def _entry_path(key: str) -> list[str]:
    """Return the lowercase setting path a top-level key sets, as the reader does."""
    parts = key.replace("__", ".").split(".")
    return [parts[0].strip().lower(), *(part.lower() for part in parts[1:])]


def _round_trip_yaml(text: str) -> YAML:
    """Return a round-trip YAML instance indented like ``text``."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = _YAML_WIDTH
    yaml.default_flow_style = False
    sequence_indent = _DEFAULT_SEQUENCE_INDENT
    sequence_offset = _DEFAULT_SEQUENCE_OFFSET
    if text.strip():
        _, guessed_indent, guessed_offset = load_yaml_guess_indent(text)
        if guessed_offset is not None:
            sequence_indent = guessed_indent
            sequence_offset = guessed_offset
    yaml.indent(
        mapping=_guess_mapping_indent(text),
        sequence=sequence_indent,
        offset=sequence_offset,
    )
    return yaml


def _guess_mapping_indent(text: str) -> int:
    """Return how far ``text`` indents a nested mapping under its key."""
    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line, following in zip(lines, lines[1:], strict=False):
        depth = len(line) - len(line.lstrip(" "))
        nested_depth = len(following) - len(following.lstrip(" "))
        nested = following.lstrip(" ")
        if (
            line.rstrip().endswith(":")
            and nested_depth > depth
            and not nested.startswith("-")
        ):
            return nested_depth - depth
    return _DEFAULT_MAPPING_INDENT


def _guess_json_indent(text: str) -> str | None:
    """Return the indent of the first indented line, or ``None`` for one line."""
    for line in text.splitlines()[1:]:
        content = line.lstrip(" \t")
        if content and len(content) < len(line):
            return line[: len(line) - len(content)]
    return None
