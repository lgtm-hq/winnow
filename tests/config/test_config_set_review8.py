"""``config set`` changes one key or refuses, and never crashes (#429).

The eighth review round's findings, each as a test: a comment placement
the round-trip parser gives up on, a comment between a key and the first
entry of a replaced collection, and text the file's encoding cannot hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config import set_config_value
from winnow.exceptions import ConfigError

_COMMENT_ERROR = "above its first entry"
_PLACE_ERROR = "the place of one of its comments cannot be established"


def _refusal(tmp_path: Path, original: str, key: str, value: object) -> str:
    """Return the message a refused set gives, checking the file is untouched."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        set_config_value(key, value, config_path=config_path)
    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(original)
    return str(error.value)


@pytest.mark.parametrize(
    "original",
    [
        "workers: 2\ncache:\n  directory: # where thumbnails go\n"
        "    /srv/cache\n\ndry_run: true\n",
        "workers: 2\ncache:\n  directory: # note\n    |\n    /srv/cache\n\n"
        "dry_run: true\n",
    ],
    ids=["plain-scalar", "block-scalar"],
)
def test_comment_the_parser_cannot_place_is_a_refusal(
    tmp_path: Path, original: str
) -> None:
    """A comment after a key whose value starts on the next line never crashes."""
    message = _refusal(tmp_path, original, "dry_run", False)

    assert_that(message).contains(_PLACE_ERROR)


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        (
            "source_dirs:\n  # main library\n  - /photos\n  - /backup\n",
            "source_dirs",
            ["/new"],
        ),
        (
            "paths:\n  # where results go\n  output_dir: /out\nworkers: 2\n",
            "paths",
            {"include_patterns": ["*.jpg"]},
        ),
        (
            "source_dirs:\n\n  # main library\n\n  - /photos\n",
            "source_dirs",
            ["/photos", "/new"],
        ),
    ],
    ids=["list", "mapping", "blank-lines-around"],
)
def test_comment_above_the_first_entry_is_refused(
    tmp_path: Path, original: str, key: str, value: object
) -> None:
    """A comment under the key describes the entries being replaced."""
    message = _refusal(tmp_path, original, key, value)

    assert_that(message).contains(_COMMENT_ERROR)


def test_comment_on_the_key_line_is_kept_when_the_list_is_replaced(
    tmp_path: Path,
) -> None:
    """A comment on the key's own line still describes the key."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("source_dirs:  # libraries\n  - /photos\n", encoding="utf-8")

    set_config_value("source_dirs", ["/new", "/more"], config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(
        "source_dirs:  # libraries\n  - /new\n  - /more\n",
    )


def test_blank_line_above_the_first_entry_does_not_block(tmp_path: Path) -> None:
    """Only a comment blocks; a blank line under the key stays where it is."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("source_dirs:\n\n  - /photos\n", encoding="utf-8")

    set_config_value("source_dirs", ["/new"], config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(
        "source_dirs:\n\n  - /new\n",
    )


def test_lone_surrogate_in_a_json_file_is_a_write_error(tmp_path: Path) -> None:
    """Text UTF-8 cannot hold is a config error, and the file is untouched."""
    config_path = tmp_path / "c.json"
    original = '{"workers": 2, "note": "\\ud800"}\n'
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigError):
        set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(original)
    assert_that([path.name for path in tmp_path.iterdir()]).is_equal_to(["c.json"])
