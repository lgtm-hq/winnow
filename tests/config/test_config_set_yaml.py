"""``config set`` edits a YAML file's text in place or refuses it (#429).

Anchors, aliases and merge keys, document markers, leading comments,
indents, and layouts the old whole-file dump could not keep.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config import CONFIG_FILE_NAME, set_config_value
from winnow.exceptions import ConfigError


def _set(tmp_path: Path, key: str, value: object) -> None:
    """Run ``set_config_value`` with discovery rooted at ``tmp_path``."""
    set_config_value(key, value, cwd=tmp_path, home_config_dir=tmp_path / "home")


def _main_file(tmp_path: Path) -> str:
    """Return the main config file's text."""
    return (tmp_path / CONFIG_FILE_NAME).read_text()


def test_comment_only_yaml_keeps_its_comments(tmp_path: Path) -> None:
    """A YAML file holding only comments keeps them when a key is added."""
    (tmp_path / CONFIG_FILE_NAME).write_text("# Winnow settings\n\n# more\n")

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to(
        "# Winnow settings\n\n# more\nworkers: 3\n",
    )


_ANCHORED = "base: &b\n  review: A\nrouting: *b\nworkers: 2\n"


@pytest.mark.parametrize("key", ["routing.review", "base.review", "base.x.y"])
def test_key_shared_through_an_anchor_is_refused(tmp_path: Path, key: str) -> None:
    """A key inside an anchored mapping that an alias shares is not edited."""
    (tmp_path / CONFIG_FILE_NAME).write_text(_ANCHORED)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, key, "X")

    assert_that(str(error.value)).contains("anchor")
    assert_that(str(error.value)).contains("by hand")
    assert_that(_main_file(tmp_path)).is_equal_to(_ANCHORED)


def test_unshared_key_in_a_file_with_anchors_can_be_set(tmp_path: Path) -> None:
    """Keys outside the shared mapping stay settable."""
    (tmp_path / CONFIG_FILE_NAME).write_text(_ANCHORED)

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to(
        "base: &b\n  review: A\nrouting: *b\nworkers: 3\n",
    )


def test_parent_line_with_an_inline_comment_keeps_its_indent(tmp_path: Path) -> None:
    """A mapping whose key line ends in a comment is not re-indented."""
    original = "cache:  # note\n    enabled: true\n"
    (tmp_path / CONFIG_FILE_NAME).write_text(original)

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to(f"{original}workers: 3\n")


@pytest.mark.parametrize(
    "original",
    [
        "cache: &a {enabled: true}\nrouting: {<<: *a}\n",
        "cache: &a\n  enabled: true\nrouting:\n  <<: [*a]\n",
    ],
)
def test_key_in_a_mapping_merged_with_a_merge_key_is_refused(
    tmp_path: Path,
    original: str,
) -> None:
    """A mapping merged elsewhere through ``<<`` counts as shared."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("cache.enabled", False, config_path=config_path)

    assert_that(str(error.value)).contains("anchor")
    assert_that(config_path.read_text()).is_equal_to(original)


def test_key_overriding_a_merge_key_can_be_set(tmp_path: Path) -> None:
    """Setting a key of the merging mapping adds an override there only."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("cache: &a {enabled: true}\nrouting: {<<: *a}\n")

    set_config_value("routing.enabled", False, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(
        "cache: &a {enabled: true}\nrouting: {<<: *a, enabled: false}\n",
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("# header\n---\nworkers: 2\n", "# header\n---\nworkers: 3\n"),
        ("# header\n---\n", "# header\n---\nworkers: 3\n"),
        ("# header\nnull\n", "# header\nworkers: 3\n"),
        ("# header\n---\nnull\n", "# header\n---\nworkers: 3\n"),
        ("# header\n{}\n", "# header\n{workers: 3}\n"),
        ("# header", "# header\nworkers: 3"),
        ("# header\n---", "# header\n---\nworkers: 3"),
        (
            "# a\n\n# b\n--- # start\nworkers: 2\n",
            "# a\n\n# b\n--- # start\nworkers: 3\n",
        ),
    ],
)
def test_leading_comments_are_kept(tmp_path: Path, before: str, after: str) -> None:
    """Comments above a document start, ``null`` or ``{}`` survive a set.

    A ``null`` document becomes the new key; ``{}`` takes it as a flow entry.
    """
    config_path = tmp_path / "c.yaml"
    config_path.write_text(before)

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(after)


@pytest.mark.parametrize(
    ("original", "after"),
    [
        ("# h\n%YAML 1.2\n---\nworkers: 2\n", "# h\n%YAML 1.2\n---\nworkers: 3\n"),
        ("--- {workers: 2}\n", "--- {workers: 3}\n"),
        ("workers: 2\n...\n", "workers: 3\n...\n"),
        ("workers: 2\n... # end\n# tail\n", "workers: 3\n... # end\n# tail\n"),
    ],
)
def test_yaml_directives_and_document_markers_are_kept(
    tmp_path: Path,
    original: str,
    after: str,
) -> None:
    """Directives and document markers outside the value stay as they are."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original)

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(after)


def test_new_key_goes_before_a_document_end(tmp_path: Path) -> None:
    """A new key is added inside the document, above its ``...``."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: 2\n...\n")

    set_config_value("dry_run", True, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(
        "workers: 2\ndry_run: true\n...\n",
    )


@pytest.mark.parametrize(
    ("before", "key", "value", "after"),
    [
        (
            "paths:\n  output_dir:\nsource_dirs: # my photos\n- /photos\n",
            "source_dirs",
            ["/photos", "/videos"],
            "paths:\n  output_dir:\nsource_dirs: # my photos\n- /photos\n- /videos\n",
        ),
        (
            "note: |\n  - # just text\nworkers: 2\n",
            "workers",
            3,
            "note: |\n  - # just text\nworkers: 3\n",
        ),
        (
            "note: |\n  key:\n  - text\nsource_dirs:\n    - /a\n",
            "source_dirs",
            ["/a", "/b"],
            "note: |\n  key:\n  - text\nsource_dirs:\n    - /a\n    - /b\n",
        ),
    ],
)
def test_sequence_indent_is_read_from_the_file(
    tmp_path: Path,
    before: str,
    key: str,
    value: object,
    after: str,
) -> None:
    """Bare dashes, comments after a key and block scalars do not mislead the guess."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(before)

    set_config_value(key, value, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(after)


@pytest.mark.parametrize(
    "original",
    [
        "source_dirs:\n  -\n    /photos\nworkers: 2\n",
        "note: >+\n  text\nworkers: 2\n",
        "cache:\n    enabled: true\nnote: |\n  text\nworkers: 2\n",
        "workers:    2\ndry_run:    false\n",
        "paths:\n  output_dir: &n\n  quarantine_dir: *n\nworkers: 2\n",
    ],
)
def test_layout_the_old_dump_changed_is_now_edited(
    tmp_path: Path,
    original: str,
) -> None:
    """Bare dashes, chomping, block indents, aligned values and anchored nulls.

    The old whole-file dump rewrote these lines, so they were refused; the
    splice never touches them.
    """
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original)

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(
        original.replace("workers: 2", "workers: 3").replace(
            "workers:    2",
            "workers:    3",
        ),
    )


def test_shared_value_is_refused_beside_a_bare_dash(tmp_path: Path) -> None:
    """A value an alias shares is refused with the anchor message."""
    config_path = tmp_path / "c.yaml"
    original = "workers: &w 2\nother: *w\nlist:\n  -\n    a\n"
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(str(error.value)).contains("shared through a YAML anchor")
    assert_that(config_path.read_text()).is_equal_to(original)


@pytest.mark.parametrize("ending", ["\n\n", "\n\n\n"])
def test_blank_lines_at_the_end_are_kept(tmp_path: Path, ending: str) -> None:
    """Blank lines after the last key survive a set."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(f"workers: 2{ending}")

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(f"workers: 3{ending}")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "---\n# comment after doc start\n",
            "---\n# comment after doc start\nworkers: 3\n",
        ),
        (
            "---\n# comment after doc start\nnull\n",
            "---\n# comment after doc start\nworkers: 3\n",
        ),
        ("# header\n---\n\nworkers: 2\n", "# header\n---\n\nworkers: 3\n"),
        ("--- # s\n\n# c\nworkers: 2\n", "--- # s\n\n# c\nworkers: 3\n"),
    ],
)
def test_comments_after_the_document_start_are_kept(
    tmp_path: Path,
    before: str,
    after: str,
) -> None:
    """Blank and comment lines after one ``---`` survive a set."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(before)

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(after)


@pytest.mark.parametrize(
    ("original", "after"),
    [
        ("null\n# footer\n", "workers: 3\n# footer\n"),
        ("{}\n# footer\n", "{workers: 3}\n# footer\n"),
        ("~\n# footer\n", "workers: 3\n# footer\n"),
        ("{} # inline\n", "{workers: 3} # inline\n"),
        ("--- ~\n", "---\nworkers: 3\n"),
    ],
)
def test_empty_document_with_a_comment_takes_the_key(
    tmp_path: Path,
    original: str,
    after: str,
) -> None:
    """A ``null`` or ``{}`` document keeps the comments around it."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original)

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(after)


@pytest.mark.parametrize(
    "original",
    ["null # inline\n", "# header\n---\nnull # inline\n"],
)
def test_null_document_with_a_comment_after_it_is_refused(
    tmp_path: Path,
    original: str,
) -> None:
    """A comment after a ``null`` document would follow the new key instead."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original)

    with pytest.raises(ConfigError, match="a comment inside its old value"):
        set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(original)


def test_null_document_with_a_comment_refuses_a_block(tmp_path: Path) -> None:
    """A mapping on several lines would move a comment after a ``null`` document."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("null # inline\n")

    with pytest.raises(ConfigError) as error:
        set_config_value("cache.enabled", False, config_path=config_path)

    assert_that(str(error.value)).contains("a comment inside its old value")
    assert_that(config_path.read_text()).is_equal_to("null # inline\n")


def test_second_document_is_refused(tmp_path: Path) -> None:
    """A file holding two documents is left as it is."""
    config_path = tmp_path / "c.yaml"
    original = "---\n# c\n---\nworkers: 2\n"
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(str(error.value)).contains("found another document")
    assert_that(config_path.read_text()).is_equal_to(original)


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        ("cache: &a {enabled: true}\nrouting: {<<: *a}\n", "cache", {"enabled": False}),
        ("workers: &w 2\nother: *w\n", "workers", 3),
        ("workers: &w 2\nother: *w\n", "other", 3),
        ("cache: {x: &a [1]}\nother: *a\n", "cache", {"enabled": False}),
    ],
)
def test_replacing_a_value_an_alias_refers_to_is_refused(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
) -> None:
    """A key whose value carries an anchor used elsewhere is not replaced."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value(key, value, config_path=config_path)

    assert_that(str(error.value)).contains("shared through a YAML anchor")
    assert_that(config_path.read_text()).is_equal_to(original)


def test_anchor_nothing_refers_to_does_not_block(tmp_path: Path) -> None:
    """An anchor without an alias is no reason to refuse."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: &w 2\ndry_run: false\n")

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to("workers: 3\ndry_run: false\n")
