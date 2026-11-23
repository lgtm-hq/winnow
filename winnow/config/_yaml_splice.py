"""Setting one key of a YAML config file by replacing only its value's text.

``config set`` never re-dumps a YAML file (#429). It finds the node the key
holds, takes the range of characters the value spans and puts the new
value's text in its place; a new key is a run of whole lines inserted after
the mapping that receives it. Every character outside that range stays as
it was, so no other line, comment, blank line, anchor, quote style, null or
boolean spelling, document marker or byte order mark can change.

A replaced value's range runs from the start of its node to the end of its
last scalar or flow collection, never over the comments after a block
collection. A scalar or empty collection is written on the key's line, a
non-empty collection as a block below the key, or, inside a flow
collection, in flow style. The edit is refused when the old value is an
alias, holds an anchor something outside it refers to, or lies in a mapping
an alias or merge key shares; when a comment would be dropped or moved, as
:meth:`_Planner._checked` decides; when a mapping on the key's way has an
explicit ``?`` key or a key without ``:``; when a new key would go into a
flow mapping that holds a comment; when the file holds a U+0085, U+2028 or
U+2029 line break; and when the new lines would not start at the column the
parser gives the lines around them.
"""

from __future__ import annotations

from dataclasses import dataclass

from winnow.config._editor import (
    entry_path,
    matching_key,
    nest,
    refuse_cast_string,
    refuse_flat_children,
    traverse_error,
)
from winnow.config._yaml_render import (
    file_style,
    is_block,
    render_block,
    render_entry,
    render_inline,
)
from winnow.config._yaml_tree import (
    LINE_BREAKS,
    NodeKind,
    YamlNode,
    YamlPair,
    parse_yaml,
)
from winnow.exceptions import ConfigError

_SPACE = " \t"
_UNICODE_BREAKS = "\x85\u2028\u2029"
"""Line breaks the parser and a line count could disagree about."""


@dataclass(frozen=True, slots=True)
class Splice:
    """New text for one range of a file's text."""

    start: int
    end: int
    text: str

    def apply(self, original: str) -> str:
        """Return ``original`` with ``[start, end)`` replaced by ``text``.

        Raises:
            ConfigError: If the range does not lie inside ``original``.
        """
        if not 0 <= self.start <= self.end <= len(original):
            raise ConfigError(
                "config set could not limit its edit to the key; the file is "
                "unchanged, edit it by hand",
                operation="set_config",
            )
        return original[: self.start] + self.text + original[self.end :]


def plan_edit(text: str, key_parts: list[str], value: object) -> Splice:
    """Return the splice that sets a dotted key in YAML text.

    The key is matched as the reader matches it: nested mappings and flat
    top-level ``a.b``/``a__b`` keys, any letter case, the last entry that
    takes effect.

    Args:
        text: The file's content, ``\\n`` line ends and no byte order mark.
        key_parts: The key's non-empty dot-separated parts.
        value: The new value, already made YAML-safe.

    Returns:
        The range of the text to replace and the text to put there.

    Raises:
        ConfigError: If the key cannot be set by changing only its value's
            text, as the module docstring lists.
        ruamel.yaml.YAMLError: If the text is not valid YAML.
        ValueError: If the text's top level is not a mapping, a comment
            cannot be placed, or the value cannot be written.
    """
    if any(char in _UNICODE_BREAKS for char in text):
        raise ConfigError(
            "config set cannot edit a YAML file that holds a U+0085, U+2028 "
            "or U+2029 line break, as their columns are uncertain; edit the "
            "file by hand",
            operation="set_config",
            details={"key": ".".join(key_parts)},
        )
    return _Planner(text, key_parts, value).plan()


class _Planner:
    """Finds where a key's value lies and what replaces it."""

    def __init__(self, text: str, key_parts: list[str], value: object) -> None:
        self._tree = parse_yaml(text)
        self._text = text
        self._parts = key_parts
        self._dotted = ".".join(key_parts)
        self._value = value
        self._style = file_style(self._tree)
        self._referenced = {name for name, _ in self._tree.aliases}

    def plan(self) -> Splice:
        """Return the splice for the key."""
        tree = self._tree
        if tree.documents > 1:
            raise ConfigError(
                "config set cannot edit a file that holds more than one YAML "
                "document; edit the file by hand",
                operation="set_config",
                details={"key": self._dotted},
            )
        root = tree.root
        whole = nest(self._parts[1:], self._value)
        if root is None or root.is_empty:
            end = tree.document_end
            position = (
                len(self._text) if end is None else tree.line_starts[tree.line_of(end)]
            )
            return self._lines_at(position, self._parts[0], whole, column=0)
        if root.is_null:
            return self._replace_root(root, whole)
        if root.kind is not NodeKind.MAPPING:
            message = "the top level must be a mapping"
            raise ValueError(message)
        self._refuse_shared_mapping(root, self._parts[0])
        self._refuse_irregular(root)
        names = [pair.name for pair in root.pairs]
        refuse_flat_children([name for name in names if name is not None], self._parts)
        wanted = [part.lower() for part in self._parts]
        for pair in reversed(root.pairs):
            path = None if pair.name is None else entry_path(pair.name)
            if path is None or path != wanted[: len(path)]:
                continue
            rest = self._parts[len(path) :]
            if not rest or pair.value.is_null:
                return self._replace(pair, nest(rest, self._value), flow=root.flow)
            return self._set_nested(self._child_mapping(pair), rest)
        return self._set_nested(root, self._parts)

    def _set_nested(self, mapping: YamlNode, parts: list[str]) -> Splice:
        """Return the splice that sets ``parts`` inside ``mapping``.

        Keys match case-insensitively, the last match winning; a missing
        key is added and a ``null`` on the way is replaced by the mappings
        below it.
        """
        self._refuse_irregular(mapping)
        names = [pair.name for pair in mapping.pairs]
        slot = matching_key([name for name in names if name is not None], parts[0])
        pair = next(
            (
                pair
                for pair in reversed(mapping.pairs)
                if slot is not None and pair.name == slot
            ),
            None,
        )
        value = nest(parts[1:], self._value)
        if pair is None:
            return self._insert(mapping, parts[0], value)
        if len(parts) == 1 or pair.value.is_null:
            return self._replace(pair, value, flow=mapping.flow)
        return self._set_nested(self._child_mapping(pair), parts[1:])

    def _child_mapping(self, pair: YamlPair) -> YamlNode:
        """Return the mapping an entry holds, to set a key inside it.

        Raises:
            ConfigError: If the value is an alias, a cast string, not a
                mapping, or a mapping an alias or merge key shares.
        """
        child = pair.value
        name = pair.name or ""
        if child.kind is NodeKind.ALIAS:
            raise self._shared_error(name)
        if child.kind is NodeKind.SCALAR:
            refuse_cast_string(child.value, name, self._parts)
        if child.kind is not NodeKind.MAPPING:
            raise traverse_error(name, self._parts)
        self._refuse_shared_mapping(child, name)
        return child

    def _refuse_irregular(self, mapping: YamlNode) -> None:
        """Refuse to edit a mapping with an explicit ``?`` key or no ``:``.

        Raises:
            ConfigError: If ``mapping`` has a key written ``? key`` or a key
                without ``:``.
        """
        if mapping.irregular:
            raise ConfigError(
                f"config set cannot change {self._dotted}: a mapping it "
                "touches has an explicit '?' key or a key without ':'; edit "
                "the file by hand",
                operation="set_config",
                details={"key": self._dotted},
            )

    def _refuse_indent(self, rendered: int, expected: int) -> None:
        """Refuse new lines that start elsewhere than the parser places them.

        Raises:
            ConfigError: If ``rendered``, the column the new lines start at,
                is not ``expected``: their siblings' column, or the parent
                key's column plus the file's indent.
        """
        if rendered != expected:
            raise ConfigError(
                f"config set cannot write {self._dotted} at the file's "
                "indentation; the file is unchanged, edit it by hand",
                operation="set_config",
                details={"key": self._dotted, "column": rendered},
            )

    def _refuse_shared_mapping(self, mapping: YamlNode, name: str) -> None:
        """Refuse to change a mapping an alias or merge key refers to."""
        if mapping.anchor is not None and mapping.anchor in self._referenced:
            raise self._shared_error(name)

    def _insert(self, mapping: YamlNode, key: str, value: object) -> Splice:
        """Return the splice that adds a new entry to a mapping.

        In a block mapping the entry's lines go after the line that ends the
        mapping's last value, past comment lines indented deeper than its
        keys, which belong to that value; in a flow mapping the entry goes
        after the last one.
        """
        tree = self._tree
        if mapping.flow:
            return self._insert_flow(mapping, key, value)
        column = tree.column_of(mapping.pairs[0].key.start)
        for sibling in (mapping.pairs[0], mapping.pairs[-1]):
            self._refuse_indent(column, sibling.key.column)
        position = tree.next_line(tree.leaf_end(mapping, kept_breaks=True) - 1)
        while position < len(self._text) and tree.comment_line(position):
            line = self._text[position : tree.line_end(position)]
            if len(line) - len(line.lstrip(_SPACE)) <= column:
                break
            position = tree.next_line(position)
        return self._lines_at(position, key, value, column=column)

    def _insert_flow(self, mapping: YamlNode, key: str, value: object) -> Splice:
        """Return the splice that adds an entry at the end of a flow mapping.

        The entry goes after the last one, or after the comma that ends it.
        After an empty last value a space keeps the ``:`` out of the last
        key, which a plain key in a flow mapping would otherwise take in; a
        key the parser reads with its ``:``, as ``{review:}``, keeps it.

        Raises:
            ConfigError: If the mapping holds a comment, which would then
                follow the new entry instead of the one it was written after.
        """
        if self._tree.comments_in(mapping.start, mapping.end):
            raise ConfigError(
                f"config set cannot add {self._dotted}: the flow mapping it "
                "goes in holds a comment, which would then follow the new "
                "key; edit the file by hand",
                operation="set_config",
                details={"key": self._dotted},
            )
        entry = (
            f"{render_inline(key, self._style, flow=True)}: "
            f"{render_inline(value, self._style, flow=True)}"
        )
        closing = mapping.end - 1
        if not mapping.pairs:
            return Splice(closing, closing, entry)
        last = mapping.pairs[-1]
        position = (
            last.colon_end if last.value.is_empty else self._tree.leaf_end(last.value)
        )
        after = self._text[position:closing].lstrip(_SPACE + LINE_BREAKS)
        if after.startswith(","):
            comma = closing - len(after) + 1
            return Splice(comma, comma, f" {entry}")
        if last.value.is_empty:
            gap = self._text[position:closing]
            spaces = len(gap) - len(gap.lstrip(_SPACE))
            space = "" if last.colon_end == last.key.end else " "
            return Splice(position, position + spaces, f"{space}, {entry}")
        return Splice(position, position, f", {entry}")

    def _lines_at(
        self, position: int, key: str, value: object, *, column: int
    ) -> Splice:
        """Return the splice that inserts an entry's lines at a line start.

        At the end of a file that does not end in a line break, the lines go
        after a new break and the file still ends without one.
        """
        lines = render_entry(key, value, self._style, column=column)
        text = self._text
        if position == len(text) and text and text[-1] not in LINE_BREAKS:
            return Splice(position, position, f"\n{lines[:-1]}")
        return Splice(position, position, lines)

    def _replace_root(self, root: YamlNode, value: object) -> Splice:
        """Return the splice that puts a mapping in place of a ``null`` document."""
        tree = self._tree
        end = tree.leaf_end(root)
        lines = render_entry(self._parts[0], value, self._style, column=0)[:-1]
        start = root.start
        if tree.column_of(start) != 0:
            start = len(self._text[:start].rstrip(_SPACE))
            lines = f"\n{lines}"
        return self._checked(
            Splice(start, end, lines),
            self._parts[0],
            old_inline=False,
            new_inline=False,
        )

    def _replace(self, pair: YamlPair, value: object, *, flow: bool) -> Splice:
        """Return the splice that replaces an entry's value.

        A block collection replaced by one of its kind keeps its place and
        column: its first key's or first ``-``'s column, which an anchor or
        tag on a line of its own above it does not share.

        Raises:
            ConfigError: If the old value is an alias, or its first key or
                ``-`` is not at the column the replaced text starts at.
        """
        tree = self._tree
        old = pair.value
        name = pair.name or ""
        if old.kind is NodeKind.ALIAS:
            raise self._shared_error(name)
        for inner in tree.nodes(old):
            self._refuse_irregular(inner)
        colon = pair.colon_end
        end = tree.leaf_end(old)
        on_key_line = tree.line_of(old.start) == tree.line_of(max(colon - 1, 0))
        old_block = old.kind is not NodeKind.SCALAR and not old.flow
        old_inline = (
            on_key_line
            and not old.is_block_scalar
            and (old.kind is NodeKind.SCALAR or old.flow)
            and not any(char in LINE_BREAKS for char in self._text[old.start : end])
        )
        if flow:
            inline = render_inline(value, self._style, flow=True)
            splice = Splice(old.start, end, f" {inline}" if old.is_empty else inline)
        elif is_block(value):
            key_column = tree.column_of(pair.key.start)
            same_kind = (old.kind is NodeKind.MAPPING) == isinstance(value, dict)
            if old_block and same_kind and not on_key_line:
                column = tree.column_of(old.start)
                self._refuse_indent(
                    column,
                    (
                        old.pairs[0].key.column
                        if old.pairs
                        else tree.first_entry_column(old)
                    ),
                )
                lines = render_block(
                    value,
                    self._style,
                    key_column=key_column,
                    column=column,
                )
                splice = Splice(old.start, end, "\n".join(lines)[column:])
            else:
                lines = render_block(value, self._style, key_column=key_column)
                indent = (
                    self._style.mapping
                    if isinstance(value, dict)
                    else self._style.offset
                )
                self._refuse_indent(
                    len(lines[0]) - len(lines[0].lstrip(" ")),
                    pair.key.column + indent,
                )
                splice = Splice(colon, end, "\n" + "\n".join(lines))
        else:
            inline = render_inline(value, self._style, flow=False)
            if old.is_empty or old_block or not on_key_line:
                splice = Splice(colon, end, f" {inline}")
            else:
                splice = Splice(old.start, end, inline)
        return self._checked(
            splice,
            name,
            old_inline=old_inline,
            new_inline=flow or not is_block(value),
        )

    def _checked(
        self,
        splice: Splice,
        name: str,
        *,
        old_inline: bool,
        new_inline: bool,
    ) -> Splice:
        """Return a replacing splice once its anchors and comments allow it.

        The comment rule: a comment strictly inside the replaced text is
        always refused, as the edit would drop it. A comment after the
        replaced text on its last line is kept only when the old and the
        new value are each a scalar or flow collection written on the
        key's own line, so the comment still follows the same key; a block
        scalar's header, a block collection and a value below its key are
        not. In every other case such a comment is refused, as it would
        then follow another key or item. Comments on other lines are not
        touched.

        When no comment follows an old value that is not on the key's own
        line, the spaces after it on its last line are replaced with it, so
        they do not trail the new value.

        Args:
            splice: The replacement.
            name: The key's name, for the error.
            old_inline: Whether the old value is a scalar or flow collection
                on the key's own line, other than a block scalar.
            new_inline: Whether the new value is written on that line.

        Raises:
            ConfigError: If the replaced text holds an anchor that something
                outside it refers to, or the comment rule refuses the edit.
        """
        tree = self._tree
        start, end = splice.start, splice.end
        inside = {node.anchor for node in tree.anchored if start <= node.start < end}
        if any(
            name_used in inside and not start <= position < end
            for name_used, position in tree.aliases
        ):
            raise self._shared_error(name)
        line_end = max(tree.line_end(end), end)
        trailing = tree.comments_in(end, line_end)
        same_line = old_inline and new_inline
        if tree.comments_in(start, end) or (trailing and not same_line):
            raise ConfigError(
                f"config set cannot replace {self._dotted}: a comment inside its "
                "old value, or after it on the same line, would be dropped or "
                "moved; edit the file by hand",
                operation="set_config",
                details={"key": self._dotted},
            )
        if not old_inline and not self._text[end:line_end].strip(_SPACE):
            return Splice(start, line_end, splice.text)
        return splice

    def _shared_error(self, name: str) -> ConfigError:
        """Return the error for a key that a YAML anchor shares."""
        return ConfigError(
            f"config set cannot change {self._dotted}: {name} is shared through "
            "a YAML anchor, so the change would reach every alias of it; edit "
            "the file by hand",
            operation="set_config",
            details={"key": self._dotted, "segment": name},
        )
