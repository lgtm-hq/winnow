"""Keeping comments in place and checking that an edit stayed in the key (#429).

The round-trip check dumps the file unedited, so it cannot see damage the
edit itself does; the post-edit check compares the text before and after
the edit, and the comments of a replaced value are moved to the new one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config._comments import edit_stays_in_place
from winnow.config._editor import dump_document, load_document, set_in_document
from winnow.config._files import ConfigFormat


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("a: 1\nb: 2\n", "a: 1\nb: 3\n"),
        ("a:\n  - x\n# c\nb: 2\n", "a:\n  - x\n  - y\n# c\nb: 2\n"),
        ("a: 1  # c\n", "a:\n  - x # c\n"),
        ("# c", "# c\na: 1"),
        ("", "a: 1\n"),
    ],
)
def test_edit_within_the_key_passes(before: str, after: str) -> None:
    """One run of key lines, comments kept, passes."""
    assert_that(edit_stays_in_place(before, after, blocks=1)).is_true()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("a:\n  - x\n# c\nb: 2\n", "a:\n  - x\n  - y\nb: 2\n"),
        ("a:\n  - x\n\nb: 2\n", "a:\n  - x\n  - y\nb: 2\n"),
        ("a: ~\n# c\nb: 2\n", "a:\n# c\n  - x\nb: 2\n"),
        ("a:\n  x: 1\n\n", "a:\n  x: 1\n\nb: 3\n\n"),
        ("a:\n  - x  # c\n  - y\n", "a:\n  - z\n"),
        ("a: 1\n", "a: 2"),
        ("a: 1", "a: 2\n"),
        ("a: 1\nb: 2\nc: 3\n", "a: 2\nb: 2\nc: 4\n"),
    ],
)
def test_edit_that_reaches_other_lines_fails(before: str, after: str) -> None:
    """Removed or added comments and blank lines, lost inline comments, a
    changed final newline, and changes in two places fail.
    """
    assert_that(edit_stays_in_place(before, after, blocks=1)).is_false()


def test_each_key_written_may_change_its_own_run() -> None:
    """Two keys written may change two separate runs."""
    before = "a: 1\nb: 2\nc: 3\n"
    after = "a: 2\nb: 2\nc: 4\n"

    assert_that(edit_stays_in_place(before, after, blocks=2)).is_true()


def _edited(text: str, key: str, value: object) -> str:
    """Return YAML content with one key set, as ``config set`` dumps it."""
    document = load_document(text, ConfigFormat.YAML, path=Path("c.yaml"))
    set_in_document(document, key.split("."), value)
    return dump_document(document, ConfigFormat.YAML, original=text)


@pytest.mark.parametrize(
    ("text", "key", "value", "after"),
    [
        (
            "extra:\n  a: 1  # c\nworkers: 2\n",
            "extra",
            [1],
            "extra:\n  - 1   # c\nworkers: 2\n",
        ),
        (
            "extra:\n  - a: 1  # c\n    b: 2\nworkers: 2\n",
            "extra",
            [{"a": 3, "b": 2}],
            "extra:\n  - a: 3  # c\n    b: 2\nworkers: 2\n",
        ),
        (
            "organize:\n  x: 1\n# c\nworkers: 2\n",
            "organize",
            {},
            "organize: {}\n# c\nworkers: 2\n",
        ),
    ],
)
def test_comments_follow_a_value_of_another_shape(
    text: str,
    key: str,
    value: object,
    after: str,
) -> None:
    """Comments follow a new value whatever its shape, and kept items keep theirs."""
    assert_that(_edited(text, key, value)).is_equal_to(after)


def test_comment_with_no_line_left_fails_the_check() -> None:
    """A last item's comment that meets the key's own comment is not kept."""
    text = "extra:  # open\n  - /a  # i\nworkers: 2\n"

    after = _edited(text, "extra", "/x")

    assert_that(after).is_equal_to("extra: /x # open\nworkers: 2\n")
    assert_that(edit_stays_in_place(text, after, blocks=1)).is_false()
