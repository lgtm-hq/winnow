"""``config set`` changes the edited key's lines and nothing around them (#429).

The fourth review round found edits that passed the round-trip check yet
deleted or moved the comments and blank lines next to the key, an alias
used as a mapping key that the anchor check missed, quoted text read as a
null, capitalised booleans refused, and a blank line written twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config import set_config_value
from winnow.exceptions import ConfigError

_LAYOUT_ERROR = "without changing its layout"


def _edit(tmp_path: Path, original: str, key: str, value: object) -> str:
    """Set a key in a file holding ``original`` and return the file's text."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")
    set_config_value(key, value, config_path=config_path)
    return config_path.read_text(encoding="utf-8")


def _refusal(tmp_path: Path, original: str, key: str, value: object) -> str:
    """Return the error a refused set gives, checking the file is untouched."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        set_config_value(key, value, config_path=config_path)
    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(original)
    return str(error.value)


@pytest.mark.parametrize(
    ("original", "key", "value", "after"),
    [
        (
            "source_dirs:\n  - /a\n# about workers\nworkers: 2\n",
            "source_dirs",
            ["/a", "/b"],
            "source_dirs:\n  - /a\n  - /b\n# about workers\nworkers: 2\n",
        ),
        (
            "source_dirs:\n  - /a\n\nworkers: 2\n",
            "source_dirs",
            ["/a", "/b"],
            "source_dirs:\n  - /a\n  - /b\n\nworkers: 2\n",
        ),
        (
            "source_dirs:\n  - /a\n# footer\n",
            "source_dirs",
            ["/a", "/b"],
            "source_dirs:\n  - /a\n  - /b\n# footer\n",
        ),
        (
            "routing:\n  review: Review\n# caching\ncache:\n  enabled: true\n",
            "routing",
            {"review": "Later"},
            "routing:\n  review: Later\n# caching\ncache:\n  enabled: true\n",
        ),
        (
            "source_dirs: null\n# about workers\nworkers: 2\n",
            "source_dirs",
            ["/a"],
            "source_dirs:\n  - /a\n# about workers\nworkers: 2\n",
        ),
        (
            "cache: null\n# about workers\nworkers: 2\n",
            "cache.enabled",
            True,
            "cache:\n  enabled: true\n# about workers\nworkers: 2\n",
        ),
        (
            "cache:\n  enabled: true\n# about workers\nworkers: 2\n",
            "cache.directory",
            "/c",
            "cache:\n  enabled: true\n  directory: /c\n# about workers\nworkers: 2\n",
        ),
    ],
)
def test_comments_and_blank_lines_next_to_the_key_stay_put(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
    after: str,
) -> None:
    """A comment or blank line after the old value follows the new one."""
    assert_that(_edit(tmp_path, original, key, value)).is_equal_to(after)


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        (
            "cache:\n  enabled: true\n  # mid\n  directory: /c\nworkers: 2\n",
            "cache",
            {"enabled": False},
        ),
        (
            "source_dirs:\n  - /a  # old\n  - /b\nworkers: 2\n",
            "source_dirs",
            ["/c"],
        ),
        (
            "cache:\n  enabled: true\n  # where\n  directory: /c\nworkers: 2\n",
            "cache",
            {"enabled": False, "directory": "/d"},
        ),
    ],
)
def test_comment_the_edit_cannot_keep_in_place_is_refused(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
) -> None:
    """A comment inside the replaced value that would be lost is refused.

    So is an edit whose changes would sit on both sides of a kept comment.
    """
    assert_that(_refusal(tmp_path, original, key, value)).contains(_LAYOUT_ERROR)


def test_list_item_comment_is_kept_on_a_kept_item(tmp_path: Path) -> None:
    """An item the new list keeps keeps its comment."""
    after = _edit(
        tmp_path,
        "source_dirs:\n  - /a  # main\n  - /b\nworkers: 2\n",
        "source_dirs",
        ["/a", "/c"],
    )

    assert_that(after).is_equal_to(
        "source_dirs:\n  - /a  # main\n  - /c\nworkers: 2\n",
    )


def test_alias_used_as_a_mapping_key_is_refused(tmp_path: Path) -> None:
    """A value an alias names as a key is shared, so it is not rewritten."""
    message = _refusal(
        tmp_path,
        "routing:\n  review: &k workers\n*k : 2\n",
        "routing.review",
        "Check",
    )

    assert_that(message).contains("shared through a YAML anchor")


@pytest.mark.parametrize(
    "original",
    [
        'paths:\n  output_dir:\nnote: "[null]"\nworkers: 2\n',
        'paths:\n  output_dir:\nnote: "a: null, b: 1"\nworkers: 2\n',
    ],
)
def test_quoted_null_text_is_not_a_null(tmp_path: Path, original: str) -> None:
    """Quoted text that looks like a null does not set the null spelling."""
    after = _edit(tmp_path, original, "workers", 3)

    assert_that(after).is_equal_to(original.replace("workers: 2", "workers: 3"))


def test_null_is_written_empty_beside_quoted_null_text(tmp_path: Path) -> None:
    """A file whose only ``~`` is quoted text gets an empty null."""
    after = _edit(tmp_path, 'note: "[~]"\nworkers: 2\n', "paths.output_dir", None)

    assert_that(after).is_equal_to('note: "[~]"\nworkers: 2\npaths:\n  output_dir:\n')


@pytest.mark.parametrize(
    ("original", "key", "value", "after"),
    [
        ("dry_run: True\nworkers: 2\n", "workers", 3, "dry_run: True\nworkers: 3\n"),
        (
            "dry_run: True\nworkers: 2\n",
            "dry_run",
            False,
            "dry_run: False\nworkers: 2\n",
        ),
        ("dry_run: TRUE\n", "recursive", False, "dry_run: TRUE\nrecursive: FALSE\n"),
        ("dry_run: true\n", "recursive", True, "dry_run: true\nrecursive: true\n"),
    ],
)
def test_booleans_keep_the_file_letter_case(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
    after: str,
) -> None:
    """Booleans are written in the letter case the file writes them."""
    assert_that(_edit(tmp_path, original, key, value)).is_equal_to(after)


def test_booleans_in_mixed_letter_case_are_refused(tmp_path: Path) -> None:
    """A file that mixes ``True`` and ``false`` cannot be written back as it was."""
    message = _refusal(tmp_path, "dry_run: True\nrecursive: false\n", "workers", 3)

    assert_that(message).contains(_LAYOUT_ERROR)


def test_new_key_adds_no_blank_line(tmp_path: Path) -> None:
    """A key added after a trailing blank line does not repeat the blank line."""
    after = _edit(tmp_path, "cache:\n  enabled: true\n\n", "workers", 3)

    assert_that(after).is_equal_to("cache:\n  enabled: true\nworkers: 3\n\n")
