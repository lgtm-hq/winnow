"""Keeping comments and blank lines in place when ``config set`` edits YAML.

``ruamel.yaml`` keeps the comments and blank lines after a value on the
value's last entry, so replacing the value, or adding a key after it, would
drop them or pull them inside the key (#429). These helpers move them to
follow the new value, and check, after the edit, that no line outside the
key changed.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from typing import Final

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.tokens import CommentToken

_MAPPING_COMMENT: Final = 2
"""Where a mapping entry keeps the comment after its value."""
_SEQUENCE_COMMENT: Final = 0
"""Where a sequence item keeps the comment after it."""
_INLINE_COMMENT: Final = re.compile(r"\s+#.*$")
"""A YAML comment after content on the same line."""
_LINE_ENDS: Final = "\n\r\x85\u2028\u2029"
"""The characters that end a line for the YAML parser."""


def edit_stays_in_place(before: str, after: str, *, blocks: int) -> bool:
    """Return whether an edit changed only the lines of the keys it wrote.

    The round-trip check reads the file unedited, so it cannot see what the
    edit itself does to the lines around a key. Compared line by line, the
    changes must form at most ``blocks`` runs of adjacent lines; none of the
    lines removed or added may be blank or a comment, so those stay where
    they were; and every comment at the end of a removed line must be on an
    added one. The file must still end in a line end, or still not.

    Args:
        before: The file's content before the edit.
        after: The content the edit gives.
        blocks: How many keys the edit wrote.

    Returns:
        ``True`` when the edit stays within those lines.
    """
    if before and after:
        ended = before[-1] in _LINE_ENDS
        if ended != (after[-1] in _LINE_ENDS):
            return False
        if not ended:
            before, after = f"{before}\n", f"{after}\n"
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    # Equal blocks separate the other opcodes, so each one is a run.
    runs = 0
    removed: list[str] = []
    added: list[str] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        runs += 1
        removed += old[old_start:old_end]
        added += new[new_start:new_end]
    if runs > blocks or any(map(_layout_line, removed + added)):
        return False
    added_text = "".join(added)
    return all(
        match.group().strip() in added_text
        for line in removed
        if (match := _INLINE_COMMENT.search(line.rstrip(_LINE_ENDS))) is not None
    )


def _layout_line(line: str) -> bool:
    """Return whether a line is blank or holds only a comment."""
    content = line.strip()
    return not content or content.startswith("#")


def replace_keeping_comments(
    mapping: CommentedMap,
    key: object,
    value: object,
) -> None:
    """Set ``key`` of a round-trip mapping, keeping the comments around it.

    The comments and blank lines that follow the old value, or follow the
    mapping's last entry when ``key`` is new, are moved to follow the new
    value, so they stay where they were in the file rather than being
    dropped or pulled inside the key. Entries the new value keeps from the
    old one keep their comments.
    """
    own = [name for name, _ in mapping.non_merged_items()]
    new_value = _commented(value)
    if key not in own:
        tail = _take_tail(mapping, own[-1], whole=False) if own else None
        mapping[key] = new_value
        if tail is not None:
            _put_tail(mapping, key, tail)
        return
    old_value = mapping[key]
    container, slot, position = _tail_slot(mapping, key)
    tail = _take_tail(mapping, key, whole=False)
    carried = _carry_comments(old_value, new_value)
    mapping[key] = new_value
    comments = container.ca.items.get(slot) if container is not mapping else None
    inline = comments[position] if comments else None
    if isinstance(inline, CommentToken) and id(inline) not in carried:
        rest = tail.value if tail is not None else "\n"
        tail = CommentToken(
            inline.value.removesuffix("\n") + rest,
            inline.start_mark,
            None,
            column=0,
        )
    if tail is not None:
        _put_tail(mapping, key, tail)


def _commented(value: object) -> object:
    """Return a value with its mappings and lists in round-trip form.

    Only round-trip containers can carry the comments moved to them.
    """
    if isinstance(value, Mapping):
        mapping = CommentedMap()
        for name, item in value.items():
            mapping[name] = _commented(item)
        return mapping
    if isinstance(value, list):
        return CommentedSeq(_commented(item) for item in value)
    return value


def _carry_comments(old: object, new: object) -> set[int]:
    """Copy comments from an old block collection to the entries the new one keeps.

    A mapping entry keeps its comments when the new mapping has the same
    key, and a list item when the new list holds an equal item, or another
    block collection of the same kind, at the same place; so do the comments
    above the first entry when it is kept. Flow collections keep their
    comments on the entry that holds them and are left alone.

    Returns:
        The ids of the comment tokens copied.
    """
    old_block, new_block = _block(old), _block(new)
    if old_block is None or new_block is None:
        return set()
    pairs = _kept_entries(old_block, new_block)
    first = old_block.ca.comment
    if pairs and pairs[0] == (_first(old_block), _first(new_block)) and first:
        if first[1]:
            new_block.ca.comment = [None, list(first[1])]
    carried: set[int] = set()
    for old_key, new_key in pairs:
        entry = old_block.ca.items.get(old_key)
        if entry:
            new_block.ca.items[new_key] = list(entry)
            carried.update(id(token) for token in entry if token is not None)
        carried |= _carry_comments(old_block[old_key], new_block[new_key])
    return carried


def _kept_entries(
    old: CommentedMap | CommentedSeq,
    new: CommentedMap | CommentedSeq,
) -> list[tuple[object, object]]:
    """Return the keys or indexes of the old entries the new collection keeps."""
    if isinstance(old, CommentedMap) and isinstance(new, CommentedMap):
        own = {name for name, _ in old.non_merged_items()}
        return [(name, name) for name in new if name in own]
    if isinstance(old, CommentedSeq) and isinstance(new, CommentedSeq):
        return [
            (index, index)
            for index in range(min(len(old), len(new)))
            if _same_item(old[index], new[index])
        ]
    return []


def _same_item(old: object, new: object) -> bool:
    """Return whether a new list item stands where an old one did."""
    old_block, new_block = _block(old), _block(new)
    if old_block is not None and new_block is not None:
        return type(old_block) is type(new_block)
    return old_block is None and new_block is None and old == new


def _first(collection: CommentedMap | CommentedSeq) -> object:
    """Return the first key of a mapping, or index 0 of a list."""
    if isinstance(collection, CommentedMap):
        return next((name for name, _ in collection.non_merged_items()), None)
    return 0


def _block(value: object) -> CommentedMap | CommentedSeq | None:
    """Return a value when it is a round-trip collection in block style."""
    if isinstance(value, CommentedMap | CommentedSeq) and not value.fa.flow_style():
        return value
    return None


def _tail_slot(
    container: CommentedMap | CommentedSeq,
    key: object,
) -> tuple[CommentedMap | CommentedSeq, object, int]:
    """Return where the comment after an entry's last line is kept.

    ``ruamel.yaml`` keeps the comments and blank lines after a block
    collection on its last entry, and after a scalar or flow collection on
    the entry that holds it.

    Returns:
        The container, the key or index and the position in its comment
        list.
    """
    value = container[key]
    if isinstance(value, CommentedMap) and not value.fa.flow_style():
        own = [name for name, _ in value.non_merged_items()]
        if own:
            return _tail_slot(value, own[-1])
    if isinstance(value, CommentedSeq) and value and not value.fa.flow_style():
        return _tail_slot(value, len(value) - 1)
    position = (
        _MAPPING_COMMENT if isinstance(container, CommentedMap) else _SEQUENCE_COMMENT
    )
    return container, key, position


def _take_tail(
    mapping: CommentedMap,
    key: object,
    *,
    whole: bool,
) -> CommentToken | None:
    """Remove and return the comment after an entry's last line.

    Args:
        mapping: The mapping that holds the entry.
        key: The entry's key.
        whole: Whether to take the comment on the last line too; otherwise
            it stays there and only the lines after it are taken.

    Returns:
        The comment, or ``None`` when there is none to move.
    """
    container, slot, position = _tail_slot(mapping, key)
    comments = container.ca.items.get(slot)
    token = comments[position] if comments else None
    if not isinstance(token, CommentToken):
        return None
    if whole or token.value.startswith("\n"):
        comments[position] = None
        return token
    inline, newline, rest = token.value.partition("\n")
    if not rest:
        return None
    token.value = f"{inline}{newline}"
    return CommentToken(f"\n{rest}", token.start_mark, None, column=0)


def _put_tail(mapping: CommentedMap, key: object, token: CommentToken) -> None:
    """Set the comment after an entry's last line, keeping one already there.

    A comment the slot already holds, such as the one after a key whose new
    value starts on the next line, keeps its line; the moved lines follow
    it. When both hold a comment on that line, the moved one is dropped and
    the layout check refuses the edit.
    """
    container, slot, position = _tail_slot(mapping, key)
    comments = container.ca.items.setdefault(slot, [None, None, None, None])
    existing = comments[position]
    if not isinstance(existing, CommentToken):
        comments[position] = token
    elif token.value.startswith("\n"):
        existing.value = existing.value.removesuffix("\n") + token.value
