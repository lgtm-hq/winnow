"""Where each comment of a YAML text belongs, for the ``config set`` fuzz (#429).

The oracle shares no code with the editor. It finds comments with
``ruamel.yaml``'s scanner, and a block scalar's header comment from the
scalar token's line, which the scanner does not report. It finds keys and
block sequence items with ``ruamel.yaml``'s composer.

A comment belongs to a key path. A comment after other text on its line
belongs to the first key or block sequence item that starts on that line,
or, on a line where none starts, to the last one that starts before it. A
full-line comment belongs to the next key or item that follows it within
the same block, at the same or a shallower indentation than the comment; a
comment indented deeper than the next key or item, or the last thing in its
block, belongs to the block: the key path of the mapping or sequence that
encloses it, which is the last key before it, or block sequence item holding
a block collection, indented less than the comment; or the document itself.
"""

from __future__ import annotations

import bisect
import io
from collections import Counter
from collections.abc import Iterator

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from ruamel.yaml.tokens import CommentToken, ScalarToken

Path = tuple[str, ...]
_END: Path = ("<end>",)
"""The owner of an inline comment on a line before any key."""


def _comment_tokens(attached: object) -> Iterator[CommentToken]:
    """Yield the comment tokens attached to a token, however nested."""
    if isinstance(attached, CommentToken):
        yield attached
    elif isinstance(attached, list):
        for item in attached:
            yield from _comment_tokens(item)


def comment_lines(text: str) -> list[tuple[int, int, str]]:
    """Return each comment's line, column and text, in order."""
    lines = text.split("\n")
    found: set[tuple[int, int, str]] = set()
    for token in YAML().scan(io.StringIO(text)):
        for comment in _comment_tokens(getattr(token, "_comment", None)):
            line = comment.start_mark.line
            for piece in comment.value.split("\n"):
                body = piece.strip()
                if not body.startswith("#"):
                    continue
                while line < len(lines) and not lines[line].rstrip().endswith(body):
                    line += 1
                column = len(lines[line].rstrip()) - len(body)
                found.add((line, column, body))
                line += 1
        if isinstance(token, ScalarToken) and token.style in {"|", ">"}:
            header = lines[token.start_mark.line][token.start_mark.column :]
            mark = header.find("#")
            if mark > 0 and header[mark - 1] in " \t":
                column = token.start_mark.column + mark
                found.add((token.start_mark.line, column, header[mark:].strip()))
    return sorted(found)


Start = tuple[int, int, Path, bool]
"""A key's or block item's line, column and path, and whether it can enclose
a block: every key, and an item that holds a block mapping or sequence."""


def _starts(text: str) -> list[Start]:
    """Return where each key and block sequence item starts, with its path."""
    root = YAML().compose(io.StringIO(text))
    starts: list[Start] = []
    seen: set[int] = set()
    pending: list[tuple[Node, Path]] = [] if root is None else [(root, ())]
    while pending:
        node, path = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, MappingNode):
            for key, value in node.value:
                name = key.value if isinstance(key, ScalarNode) else "?"
                place = (*path, str(name))
                mark = key.start_mark
                starts.append((mark.line, mark.column, place, True))
                pending.append((value, place))
        elif isinstance(node, SequenceNode):
            for index, item in enumerate(node.value):
                place = (*path, str(index))
                if not node.flow_style:
                    mark = item.start_mark
                    block = isinstance(item, MappingNode | SequenceNode) and not (
                        item.flow_style
                    )
                    starts.append((mark.line, mark.column, place, block))
                pending.append((item, place))
    return sorted(starts)


def _enclosing(before: list[Start], column: int) -> Path:
    """Return the path of the block a comment at ``column`` sits in."""
    for _, start_column, path, encloses in reversed(before):
        if encloses and start_column < column:
            return path
    return ()


def comment_owners(text: str) -> Counter[tuple[str, Path]]:
    """Return each comment's text with the key path it belongs to, counted."""
    starts = _starts(text)
    lines = [start[0] for start in starts]
    lines_text = text.split("\n")
    owners: Counter[tuple[str, Path]] = Counter()
    for line, column, body in comment_lines(text):
        inline = bool(lines_text[line][:column].strip())
        if inline:
            low = bisect.bisect_left(lines, line)
            if low < len(starts) and starts[low][0] == line:
                owners[body, starts[low][2]] += 1
                continue
            owner = starts[low - 1][2] if low else _END
        else:
            after = bisect.bisect_right(lines, line)
            if after < len(starts) and starts[after][1] >= column:
                owner = starts[after][2]
            else:
                owner = _enclosing(starts[:after], column)
        owners[body, owner] += 1
    return owners
