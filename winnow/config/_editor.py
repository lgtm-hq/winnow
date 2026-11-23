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
from collections.abc import Mapping, MutableMapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, merge_attrib
from ruamel.yaml.error import YAMLError

from winnow.config._comments import replace_keeping_comments
from winnow.config._files import ConfigFormat, config_format
from winnow.config._markers import cast_marker
from winnow.config._yaml_style import round_trip_yaml
from winnow.config.schema import render_config_yaml
from winnow.exceptions import ConfigError

_DEFAULT_JSON_INDENT: Final = 2
_CRLF: Final = "\r\n"
_BOM: Final = "\ufeff"
_DOCUMENT_START: Final = re.compile(r"---(\s+#.*)?\s*")
"""A YAML document start marker alone on its line, perhaps with a comment."""
_DOCUMENT_MARKER: Final = re.compile(r"^(%|---(?=\s|$)|\.\.\.(?=\s|$))", re.MULTILINE)
"""A directive, document start or document end at the start of a line."""


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
        ConfigError: If a YAML file holds a directive, a document end, or a
            document start other than a ``---`` line after its leading
            comments, or is an empty document that holds a comment; the
            round trip would drop them.
        ValueError: If the content is invalid, a JSON object repeats a key, or
            the top level is not a mapping.
    """
    if text is None:
        return {}
    document: object
    if file_format is ConfigFormat.JSON:
        document = json.loads(text, object_pairs_hook=_unique_keys)
    else:
        body = _split_preamble(text)[1]
        if _DOCUMENT_MARKER.search(body):
            raise ConfigError(
                "config set cannot keep this file's YAML directives or "
                "document markers (%, ---, ...); edit the file by hand",
                operation="set_config",
                file_path=path,
            )
        stream = io.StringIO(body)
        stream.name = str(path)
        loader = YAML()
        loader.preserve_quotes = True
        document = loader.load(stream)
        _keep_anchors(document)
        if not document and "#" in body:
            raise ConfigError(
                "config set cannot keep the comments of an empty YAML document "
                "(null, ~ or {}); edit the file by hand",
                operation="set_config",
                file_path=path,
            )
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


_PLAIN_LAYOUT: Final = TextLayout()
"""LF line ends and no BOM: the layout of a new file."""


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
    document: MutableMapping[str, object],
    file_format: ConfigFormat,
    *,
    original: str | None,
    layout: TextLayout = _PLAIN_LAYOUT,
) -> str:
    """Render an edited document in its file's format and line layout.

    Args:
        document: The edited top-level mapping.
        file_format: The file's format, YAML or JSON.
        original: The file's content before the edit with ``\\n`` line ends
            and no BOM, or ``None`` for a new file, which is written as
            ``config set`` always wrote new files.
        layout: The file's line ends and BOM, which the new content keeps.

    Returns:
        The new file content. It ends in the newlines the original's content
        ended with, blank lines included, or in one newline when there was
        none; keeps a YAML file's leading comments and document start; and
        is laid out as ``layout`` says.

    Raises:
        ruamel.yaml.YAMLError: If YAML cannot represent a value.
        ValueError: If JSON cannot hold a value.
        TypeError: If JSON cannot serialize a value.
    """
    preamble = ""
    body = original or ""
    if file_format is ConfigFormat.JSON:
        indent = (
            _DEFAULT_JSON_INDENT if original is None else _guess_json_indent(original)
        )
        text = json.dumps(document, indent=indent, ensure_ascii=False, allow_nan=False)
    elif original is None:
        text = render_config_yaml(document)
    else:
        preamble, body = _split_preamble(original)
        stream = io.StringIO()
        round_trip_yaml(body).dump(document, stream)
        text = stream.getvalue()
        last_line = preamble.splitlines(keepends=True)[-1] if preamble else ""
        if last_line and last_line.splitlines()[0] == last_line:
            preamble = f"{preamble}\n"
    text = preamble + text.rstrip("\n") + _final_newlines(original, body)
    return layout.apply(text)


def keeps_layout(
    document: MutableMapping[str, object],
    file_format: ConfigFormat,
    *,
    original: str | None,
    layout: TextLayout,
) -> bool:
    """Return whether writing a document back unedited gives its file as it is.

    ``ruamel.yaml`` rewrites some YAML it reads, such as a bare ``-`` item, a
    block scalar's indent or ``+`` chomping, or extra spaces after a key, so
    an edit would change those lines too. JSON files are re-indented by
    design and are not checked.

    Args:
        document: The file's top-level mapping, before any edit.
        file_format: The file's format.
        original: The file's content with ``\\n`` line ends and no BOM, or
            ``None`` for a new file.
        layout: The file's line ends and BOM.

    Returns:
        ``True`` for JSON, a new file or an empty YAML document, and for a
        YAML file the dump gives back byte for byte; ``False`` otherwise.
    """
    if file_format is not ConfigFormat.YAML or original is None or not document:
        return True
    expected = layout.apply(original)
    try:
        return (
            dump_document(document, file_format, original=original, layout=layout)
            == expected
        )
    except YAMLError:
        return False


def _final_newlines(original: str | None, body: str) -> str:
    """Return the newlines that end new content.

    Args:
        original: The file's content before the edit, or ``None``.
        body: The part of it the dump replaces.

    Returns:
        One newline for a new or empty file; the newlines ``body`` ends with
        when it holds content, blank lines at the end included; otherwise one
        newline when ``original`` ends in one.
    """
    if not original:
        return "\n"
    if body.strip():
        return body[len(body.rstrip("\n")) :]
    return "\n" if original.endswith("\n") else ""


def _split_preamble(text: str) -> tuple[str, str]:
    """Split YAML content into its leading comments and the rest.

    ``ruamel.yaml`` drops comments around a ``---`` document start, above a
    ``null`` document and in a file that holds nothing else, so they are
    kept apart and written back unchanged. Lines end where the YAML parser
    ends them, U+0085, U+2028 and U+2029 included.

    Returns:
        The leading blank and comment lines, with at most one ``---`` line
        among them, and the remaining content.
    """
    lines = text.splitlines(keepends=True)
    count = 0
    started = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            count += 1
            continue
        if started or not _DOCUMENT_START.fullmatch(stripped):
            break
        started = True
        count += 1
    return "".join(lines[:count]), "".join(lines[count:])


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
    elif isinstance(value, Sequence | Set) and not isinstance(
        value,
        str | bytes | bytearray,
    ):
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
            lies in a mapping a YAML anchor shares, holds a value that carries
            an anchor something else refers to, or has a child another
            top-level flat key sets.
    """
    wanted = [part.lower() for part in key_parts]
    shared = _shared_nodes(document)
    _refuse_flat_children(document, wanted, key_parts)
    for raw_key in reversed(list(document)):
        entry_path = _entry_path(str(raw_key))
        if entry_path != wanted[: len(entry_path)]:
            continue
        rest = key_parts[len(entry_path) :]
        if not rest or document[raw_key] is None:
            _replace(document, raw_key, _nest(rest, value), key_parts, shared)
            return
        child = _child_mapping(document, raw_key, key_parts, shared)
        _set_nested(child, rest, value, key_parts, shared)
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


def _shared_nodes(document: object) -> set[int]:
    """Return the ids of values reachable along more than one path.

    A YAML alias loads as the very object its anchor names, so changing it in
    place, or replacing it, changes every place that shares it. That holds
    for mappings, lists and anchored scalars; an unanchored scalar is never
    shared, whatever its id. Mapping keys count as well, as an alias may
    name an anchored value as a key. A mapping merged into another with a
    ``<<`` merge key is shared too: ``ruamel.yaml`` keeps it in the merging
    mapping's merge list, and changing it changes that mapping.
    """
    seen: set[int] = set()
    shared: set[int] = set()

    def visit(node: object) -> None:
        merged: list[object] = []
        children: list[object] = []
        if isinstance(node, Mapping):
            children = [*node.keys(), *node.values()]
            merged = list(getattr(node, merge_attrib, ()))
        elif isinstance(node, list):
            children = list(node)
        elif _anchor_name(node) is None:
            return
        if id(node) in seen:
            shared.add(id(node))
            return
        seen.add(id(node))
        for mapping in merged:
            shared.add(id(mapping))
            visit(mapping)
        for child in children:
            visit(child)

    visit(document)
    return shared


def _keep_anchors(document: object) -> None:
    """Mark every anchor in a loaded document to be written back.

    ``ruamel.yaml`` drops an anchor on a mapping or list that no alias
    refers to; the file wrote it, so the dump keeps it.
    """
    seen: set[int] = set()
    pending = [document]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        anchor = getattr(node, "anchor", None)
        if anchor is not None and anchor.value is not None:
            anchor.always_dump = True
        if isinstance(node, Mapping):
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def _anchor_name(node: object) -> str | None:
    """Return the name of the YAML anchor a loaded value carries, if any."""
    anchor = getattr(node, "anchor", None)
    name = getattr(anchor, "value", None)
    return name if isinstance(name, str) else None


def _holds_shared(node: object, shared: set[int]) -> bool:
    """Return whether a value, or any value inside it, is shared.

    A mapping's merged keys are not its own and are not looked at; a cycle
    passes through a shared value, so the walk ends.
    """
    if id(node) in shared:
        return True
    children: list[object] = []
    if isinstance(node, CommentedMap):
        children = [value for _, value in node.non_merged_items()]
    elif isinstance(node, Mapping):
        children = list(node.values())
    elif isinstance(node, list):
        children = list(node)
    return any(_holds_shared(child, shared) for child in children)


def _replace(
    mapping: MutableMapping[str, object],
    key: str,
    value: object,
    key_parts: list[str],
    shared: set[int],
) -> None:
    """Set ``key`` of ``mapping`` to ``value``, unless an alias shares the old one.

    The comments and blank lines that follow the old value, or follow the
    mapping's last entry when ``key`` is new, are moved to follow the new
    value, so they stay where they were in the file rather than being
    dropped or pulled inside the key.

    Raises:
        ConfigError: If the value ``key`` holds carries an anchor something
            else refers to, as replacing it would rewrite that alias.
    """
    if key in mapping and _holds_shared(mapping[key], shared):
        raise _shared_error(".".join(key_parts), key)
    if isinstance(mapping, CommentedMap):
        replace_keeping_comments(mapping, key, value)
    else:
        mapping[key] = value


def _nest(parts: list[str], value: object) -> object:
    """Return ``value`` nested in one mapping per part, outermost first."""
    for part in reversed(parts):
        value = {part: value}
    return value


def _shared_error(dotted: str, key: str) -> ConfigError:
    """Return the error for a key that a YAML anchor shares."""
    return ConfigError(
        f"config set cannot change {dotted}: {key} is shared through a YAML "
        "anchor, so the change would reach every alias of it; edit the "
        "file by hand",
        operation="set_config",
        details={"key": dotted, "segment": key},
    )


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
        shared: Ids of values a YAML anchor shares.
    """
    current = mapping
    for index, part in enumerate(parts[:-1]):
        slot = _matching_key(current, part)
        if slot not in current or current[slot] is None:
            nested = _nest(parts[index + 1 :], value)
            _replace(current, slot, nested, key_parts, shared)
            return
        current = _child_mapping(current, slot, key_parts, shared)
    _replace(current, _matching_key(current, parts[-1]), value, key_parts, shared)


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
    """Return the mapping under ``key``; a ``null`` there is replaced by the caller.

    Raises:
        ConfigError: If the value under ``key`` is a cast string, a mapping a
            YAML anchor shares, or not a mapping.
    """
    child = mapping[key]
    dotted = ".".join(key_parts)
    if id(child) in shared:
        raise _shared_error(dotted, key)
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


def _guess_json_indent(text: str) -> str | None:
    """Return the indent of the first indented line, or ``None`` for one line."""
    for line in text.splitlines()[1:]:
        content = line.lstrip(" \t")
        if content and len(content) < len(line):
            return line[: len(line) - len(content)]
    return None
