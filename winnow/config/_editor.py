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
import math
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.util import load_yaml_guess_indent

from winnow.config._files import ConfigFormat, config_format
from winnow.config._markers import cast_marker
from winnow.config.schema import render_config_yaml
from winnow.exceptions import ConfigError

_DEFAULT_MAPPING_INDENT: Final = 2
_DEFAULT_SEQUENCE_INDENT: Final = 4
_DEFAULT_SEQUENCE_OFFSET: Final = 2
_DEFAULT_JSON_INDENT: Final = 2
_YAML_WIDTH: Final = 4096
"""Wide enough that the dump never folds a long value onto a new line."""
_CRLF: Final = "\r\n"
_INLINE_COMMENT: Final = re.compile(r"\s+#.*$")
"""A YAML comment after content on the same line."""


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
    *,
    path: Path,
) -> MutableMapping[str, object]:
    """Parse config file content for editing.

    Args:
        text: The file's content with ``\\n`` line ends, or ``None`` for a
            file that does not exist.
        file_format: The file's format, YAML or JSON.
        path: The file, so YAML parse errors name it.

    Returns:
        The top-level mapping, round-trip loaded for YAML; an empty mapping
        for a missing file, an empty or comment-only YAML file, or JSON
        ``null``. An empty JSON file is invalid JSON, as the reader says.

    Raises:
        ValueError: If the content is invalid, a JSON object repeats a key, or
            the top level is not a mapping.
    """
    if text is None:
        return {}
    document: object
    if file_format is ConfigFormat.JSON:
        document = json.loads(text, object_pairs_hook=_unique_keys)
    else:
        stream = io.StringIO(text)
        stream.name = str(path)
        loader = YAML()
        loader.preserve_quotes = True
        document = loader.load(stream)
    if document is None:
        return CommentedMap() if file_format is ConfigFormat.YAML else {}
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


def split_line_ends(raw: str) -> tuple[str, bool]:
    """Return file content with ``\\n`` line ends, and whether it used CRLF."""
    return raw.replace(_CRLF, "\n"), _CRLF in raw


def dump_document(
    document: MutableMapping[str, object],
    file_format: ConfigFormat,
    *,
    original: str | None,
    crlf: bool = False,
) -> str:
    """Render an edited document in its file's format and line layout.

    Args:
        document: The edited top-level mapping.
        file_format: The file's format, YAML or JSON.
        original: The file's content before the edit with ``\\n`` line ends,
            or ``None`` for a new file, which is written as ``config set``
            always wrote new files.
        crlf: Whether the file ends its lines with CRLF.

    Returns:
        The new file content. It ends in a newline when the original did or
        was empty, and uses CRLF line ends when ``crlf`` is set.
    """
    if file_format is ConfigFormat.JSON:
        indent = (
            _DEFAULT_JSON_INDENT if original is None else _guess_json_indent(original)
        )
        text = json.dumps(document, indent=indent, ensure_ascii=False, allow_nan=False)
    elif original is None:
        text = render_config_yaml(document)
    else:
        stream = io.StringIO()
        _round_trip_yaml(original).dump(document, stream)
        text = _keep_leading_comments(stream.getvalue(), original)
    text = text.rstrip("\n")
    if not original or original.endswith("\n"):
        text = f"{text}\n"
    return text.replace("\n", _CRLF) if crlf else text


def _keep_leading_comments(text: str, original: str) -> str:
    """Put back the comments of a YAML file that held nothing but comments.

    ``ruamel.yaml`` loads such a file as no document and drops its comments,
    so they are written above the new content.
    """
    lines = original.splitlines()
    comment_only = any(line.strip() for line in lines) and all(
        not line.strip() or line.lstrip().startswith("#") for line in lines
    )
    if not comment_only:
        return text
    comments = original.rstrip("\n")
    return f"{comments}\n{text}"


def check_finite(value: object, *, key: str) -> None:
    """Refuse NaN and infinite numbers anywhere in a value.

    Neither JSON nor the config model can hold them, and YAML reads them
    back as a different value.

    Raises:
        ConfigError: If ``value`` holds a float that is not finite.
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(
            f"{value} is not a finite number; config values must be finite",
            operation="set_config",
            details={"key": key},
        )
    if isinstance(value, Mapping):
        for item in value.values():
            check_finite(item, key=key)
    elif isinstance(value, list):
        for item in value:
            check_finite(item, key=key)


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
        ConfigError: If the key passes through a value that is not a mapping,
            lies in a mapping a YAML anchor shares, or has a child another
            top-level flat key sets.
    """
    wanted = [part.lower() for part in key_parts]
    shared = _shared_containers(document)
    _refuse_flat_children(document, wanted, key_parts)
    for raw_key in reversed(list(document)):
        entry_path = _entry_path(str(raw_key))
        if entry_path != wanted[: len(entry_path)]:
            continue
        if len(entry_path) == len(wanted):
            document[raw_key] = value
            return
        child = _child_mapping(document, raw_key, key_parts, shared)
        _set_nested(child, key_parts[len(entry_path) :], value, key_parts, shared)
        return
    _set_nested(document, key_parts, value, key_parts, shared)


def _refuse_flat_children(
    document: MutableMapping[str, object],
    wanted: list[str],
    key_parts: list[str],
) -> None:
    """Refuse a key when a top-level flat key sets something below it.

    The reader merges that flat key into whatever value is written, so the
    key would not end up holding the value given.

    Raises:
        ConfigError: Naming the flat key.
    """
    for raw_key in document:
        entry_path = _entry_path(str(raw_key))
        if len(entry_path) > len(wanted) and entry_path[: len(wanted)] == wanted:
            raise ConfigError(
                f"config set cannot replace {'.'.join(key_parts)}: the file also "
                f"sets {raw_key} as a separate flat key, which would be merged "
                "into the new value; set that key on its own or edit the file "
                "by hand",
                operation="set_config",
                details={"key": ".".join(key_parts), "flat_key": str(raw_key)},
            )


def _shared_containers(document: object) -> set[int]:
    """Return the ids of mappings and lists reachable along more than one path.

    A YAML alias loads as the very object its anchor names, so changing it in
    place changes every place that shares it.
    """
    seen: set[int] = set()
    shared: set[int] = set()

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            children = list(node.values())
        elif isinstance(node, list):
            children = list(node)
        else:
            return
        if id(node) in seen:
            shared.add(id(node))
            return
        seen.add(id(node))
        for child in children:
            visit(child)

    visit(document)
    return shared


def _set_nested(
    mapping: MutableMapping[str, object],
    parts: list[str],
    value: object,
    key_parts: list[str],
    shared: set[int],
) -> None:
    """Set ``parts`` inside a nested mapping, matching keys case-insensitively.

    Args:
        mapping: The mapping to set the key in; changed in place.
        parts: The key's remaining parts.
        value: The value to write.
        key_parts: The whole key, for error messages.
        shared: Ids of containers a YAML anchor shares.
    """
    current = mapping
    for part in parts[:-1]:
        slot = _matching_key(current, part)
        if slot not in current:
            current[slot] = {}
        current = _child_mapping(current, slot, key_parts, shared)
    current[_matching_key(current, parts[-1])] = value


def _matching_key(mapping: MutableMapping[str, object], part: str) -> str:
    """Return the last key of ``mapping`` equal to ``part`` ignoring case, or it."""
    existing = [key for key in mapping if str(key).lower() == part.lower()]
    return existing[-1] if existing else part


def _child_mapping(
    mapping: MutableMapping[str, object],
    key: str,
    key_parts: list[str],
    shared: set[int],
) -> MutableMapping[str, object]:
    """Return the mapping under ``key``, replacing a ``null`` with a new one.

    Raises:
        ConfigError: If the value under ``key`` is a cast string, a mapping a
            YAML anchor shares, or neither a mapping nor null.
    """
    child = mapping[key]
    if child is None:
        child = mapping[key] = {}
    dotted = ".".join(key_parts)
    if id(child) in shared:
        raise ConfigError(
            f"config set cannot change {dotted}: {key} is shared through a YAML "
            "anchor, so the change would reach every alias of it; edit the "
            "file by hand",
            operation="set_config",
            details={"key": dotted, "segment": key},
        )
    if isinstance(child, str) and cast_marker(child)[0]:
        raise ConfigError(
            f"config set cannot change {dotted}: {key} is written as a cast "
            f"string ({child.partition(' ')[0]}); set the whole {key} or edit "
            "the file by hand",
            operation="set_config",
            details={"key": dotted, "segment": key},
        )
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
            _INLINE_COMMENT.sub("", line).rstrip().endswith(":")
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
