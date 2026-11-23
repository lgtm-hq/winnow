"""``config set`` on hand-written configs changes exactly one line (#429).

Each file is written in a style people use for YAML config files. The
round-trip guard must not refuse any of them: an ordinary ``config set``
changes the one line that holds the key and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config import set_config_value
from winnow.config.schema import render_config_yaml
from winnow.exceptions import ConfigError
from winnow.models.config import WinnowConfig

_CORPUS: dict[str, tuple[str, str, object]] = {
    "flat two keys": ("workers: 2\ndry_run: false\n", "workers", 4),
    "indent 2 nested": (
        "cache:\n  enabled: true\n  directory: /data/cache\nworkers: 2\n",
        "cache.enabled",
        False,
    ),
    "indent 4 nested": (
        "cache:\n    enabled: true\n    directory: /data/cache\nworkers: 2\n",
        "cache.directory",
        "/data/other",
    ),
    "sequence offset 0": (
        "source_dirs:\n- /photos\n- /videos\nworkers: 2\n",
        "workers",
        3,
    ),
    "sequence offset 2": (
        "source_dirs:\n  - /photos\n  - /videos\nworkers: 2\n",
        "workers",
        3,
    ),
    "sequence offset 2 indent 4": (
        "routing:\n    review: Review\nsource_dirs:\n    - /photos\nworkers: 2\n",
        "routing.review",
        "Check",
    ),
    "inline comments": (
        "workers: 2  # threads\ndry_run: true # safe first\n",
        "dry_run",
        False,
    ),
    "full-line comments": (
        "# How many threads\nworkers: 2\n# Try first\ndry_run: true\n",
        "workers",
        8,
    ),
    "blank lines between sections": (
        "workers: 2\n\ncache:\n  enabled: true\n\nrouting:\n  review: Review\n",
        "routing.review",
        "Later",
    ),
    "single-quoted strings": (
        "routing:\n  review: 'Needs review'\n  graphics: 'Art'\n",
        "routing.graphics",
        "Drawings",
    ),
    "double-quoted strings": (
        'routing:\n  review: "Needs review"\n  graphics: "Art"\n',
        "routing.review",
        "Later",
    ),
    "flow list": ("source_dirs: [/photos, /videos]\nworkers: 2\n", "workers", 5),
    "flow map": ("organize: {}\ncache: {enabled: true}\nworkers: 2\n", "workers", 5),
    "set flow list": ("source_dirs: [/photos]\ndry_run: true\n", "dry_run", False),
    "long string": (
        "routing:\n  review: " + "Review " * 40 + "end\nworkers: 2\n",
        "workers",
        6,
    ),
    "non-ASCII text": (
        "# Réglages de tri\nrouting:\n  review: À revoir\n"
        "  screenshots: Captures d’écran\n",
        "routing.review",
        "Prüfen",
    ),
    "emoji value": ("routing:\n  review: 📷 Review\nworkers: 2\n", "workers", 3),
    "leading comment block": (
        "# Winnow config\n#\n# Edit with care.\n\nworkers: 2\ndry_run: true\n",
        "dry_run",
        False,
    ),
    "document start": ("---\nworkers: 2\ndry_run: true\n", "workers", 3),
    "comment and document start": (
        "# settings\n---\nworkers: 2\n",
        "workers",
        3,
    ),
    "unshared anchor": (
        "cache: &cache\n  enabled: true\nworkers: 2\n",
        "workers",
        3,
    ),
    "flat dotted key": (
        "routing.review: Review\nworkers: 2\n",
        "routing.review",
        "Later",
    ),
    "dunder key": ("routing__review: Review\nworkers: 2\n", "workers", 3),
    "cast marker kept": ('workers: "@int 2"\ndry_run: true\n', "dry_run", False),
    "float threshold": (
        "min_similarity: 0.95\nperceptual_hash_distance_threshold: 8\n",
        "min_similarity",
        0.9,
    ),
    "enum value": ("sort_order: by_quality\nworkers: 2\n", "sort_order", "by_date"),
    "empty null": ("paths:\n  output_dir:\nworkers: 2\n", "workers", 3),
    "null spelled null": ("paths:\n  output_dir: null\nworkers: 2\n", "workers", 3),
    "tilde null": ("paths:\n  output_dir: ~\nworkers: 2\n", "workers", 3),
    "capital nulls": (
        "paths:\n  output_dir: Null\n  quarantine_dir: Null\nworkers: 2\n",
        "workers",
        3,
    ),
    "null in a flow map": (
        "paths: {output_dir: null, quarantine_dir: null}\nworkers: 2\n",
        "workers",
        3,
    ),
    "tilde in a flow list": ("extra: [~, 1]\nworkers: 2\n", "workers", 3),
    "null in a block list": ("extra:\n  - null\n  - 1\nworkers: 2\n", "workers", 3),
    "word containing null": (
        'note: "null"\nname: nullable\nworkers: 2\n',
        "workers",
        3,
    ),
    "empty lists": (
        "paths:\n  include_patterns: []\n  exclude_patterns: []\nworkers: 2\n",
        "workers",
        3,
    ),
    "quoted globs": (
        "paths:\n  include_patterns:\n    - '*.jpg'\n    - \"*.png\"\nworkers: 2\n",
        "workers",
        3,
    ),
    "deep nesting": (
        "routing:\n  review: Review\n  min_confidence: 0.75\ncache:\n  enabled: true\n",
        "routing.min_confidence",
        0.5,
    ),
    "trailing blank line": ("workers: 2\ndry_run: true\n\n", "workers", 3),
    "literal block scalar": (
        "notes: |\n  Keep raw photos.\n  - not a list\nworkers: 2\n",
        "workers",
        3,
    ),
    "folded block scalar": (
        "notes: >\n  Sorted weekly\n  by hand.\nworkers: 2\n",
        "workers",
        3,
    ),
    "indent 4 block scalars": (
        "notes: |\n    Keep raw photos.\nrouting:\n    review: |-\n        Look again\n"
        "workers: 2\n",
        "workers",
        3,
    ),
    "strip chomping": ("notes: |-\n  text\nworkers: 2\n", "workers", 3),
    "booleans spelled out": ("dry_run: true\nrecursive: false\n", "recursive", True),
}
"""Name to file content, key and value, for files with ``\\n`` line ends."""

_EDIT_CORPUS: dict[str, tuple[str, str, object, str]] = {
    "list then a comment": (
        "source_dirs:\n  - /a\n# about workers\nworkers: 2\n",
        "source_dirs",
        ["/a", "/b"],
        "source_dirs:\n  - /a\n  - /b\n# about workers\nworkers: 2\n",
    ),
    "list then a blank line": (
        "source_dirs:\n  - /a\n\nworkers: 2\n",
        "source_dirs",
        ["/a", "/b"],
        "source_dirs:\n  - /a\n  - /b\n\nworkers: 2\n",
    ),
    "nested list then a sibling": (
        "paths:\n  include_patterns:\n    - '*.jpg'\n  # outputs\n  output_dir: /out\n",
        "paths.include_patterns",
        ["*.jpg", "*.png"],
        "paths:\n  include_patterns:\n    - '*.jpg'\n    - '*.png'\n"
        "  # outputs\n  output_dir: /out\n",
    ),
    "mapping then the next section": (
        "routing:\n  review: Review\n# caching\ncache:\n  enabled: true\n",
        "routing",
        {"review": "Later", "graphics": "Art"},
        "routing:\n  review: Later\n  graphics: Art\n"
        "# caching\ncache:\n  enabled: true\n",
    ),
    "list then a footer": (
        "workers: 2\nsource_dirs:\n  - /a\n# end of file\n",
        "source_dirs",
        ["/a", "/b"],
        "workers: 2\nsource_dirs:\n  - /a\n  - /b\n# end of file\n",
    ),
    "null list then a comment": (
        "source_dirs: null\n# about workers\nworkers: 2\n",
        "source_dirs",
        ["/a"],
        "source_dirs:\n  - /a\n# about workers\nworkers: 2\n",
    ),
    "null mapping then a comment": (
        "cache: null\n# about workers\nworkers: 2\n",
        "cache.enabled",
        True,
        "cache:\n  enabled: true\n# about workers\nworkers: 2\n",
    ),
    "new nested key then a comment": (
        "cache:\n  enabled: true\n# about workers\nworkers: 2\n",
        "cache.directory",
        "/data/cache",
        "cache:\n  enabled: true\n  directory: /data/cache\n"
        "# about workers\nworkers: 2\n",
    ),
    "new key after a trailing blank line": (
        "cache:\n  enabled: true\n\n",
        "workers",
        3,
        "cache:\n  enabled: true\nworkers: 3\n\n",
    ),
    "replace a list": (
        "source_dirs:\n  - /photos  # main\n  - /videos\nworkers: 2\n",
        "source_dirs",
        ["/photos", "/music"],
        "source_dirs:\n  - /photos  # main\n  - /music\nworkers: 2\n",
    ),
    "replace a mapping": (
        "cache:\n  enabled: true  # on\n  directory: /c\n\nworkers: 2\n",
        "cache",
        {"enabled": False},
        "cache:\n  enabled: false # on\n\nworkers: 2\n",
    ),
    "set a nested scalar": (
        "cache:\n  enabled: true\n\n# threads\nworkers: 2\n",
        "cache.enabled",
        False,
        "cache:\n  enabled: false\n\n# threads\nworkers: 2\n",
    ),
    "add a top-level key": (
        "workers: 2  # threads\n\n# the end\n",
        "dry_run",
        True,
        "workers: 2  # threads\ndry_run: true\n\n# the end\n",
    ),
    "add a nested key": (
        "routing:\n  review: Review\n\nworkers: 2\n",
        "routing.graphics",
        "Art",
        "routing:\n  review: Review\n  graphics: Art\n\nworkers: 2\n",
    ),
    "set a key to null": (
        "paths:\n  output_dir: /out  # here\n  quarantine_dir: ~\n",
        "paths.output_dir",
        None,
        "paths:\n  output_dir: ~     # here\n  quarantine_dir: ~\n",
    ),
    "replace null with a list": (
        "source_dirs:  # none yet\nworkers: 2\n",
        "source_dirs",
        ["/a", "/b"],
        "source_dirs:  # none yet\n  - /a\n  - /b\nworkers: 2\n",
    ),
    "replace null with a mapping": (
        "workers: 2\ncache: ~\n\n# end\n",
        "cache",
        {"enabled": True},
        "workers: 2\ncache:\n  enabled: true\n\n# end\n",
    ),
    "capitalised booleans": (
        "dry_run: True\nrecursive: False\n",
        "recursive",
        True,
        "dry_run: True\nrecursive: True\n",
    ),
}
"""Name to file content, key, value and the content the edit gives.

The review"s reproductions of comments and blank lines next to an edited
key, and one file per edit shape the fuzz makes.
"""


def _changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    """Return the lines that differ; the line counts must be equal."""
    old, new = before.splitlines(), after.splitlines()
    assert_that(new).is_length(len(old))
    pairs = zip(old, new, strict=True)
    return [(left, right) for left, right in pairs if left != right]


@pytest.mark.parametrize("name", list(_CORPUS))
def test_ordinary_file_changes_one_line(tmp_path: Path, name: str) -> None:
    """The set changes the key's line and nothing else."""
    original, key, value = _CORPUS[name]
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")

    set_config_value(key, value, config_path=config_path)

    changed = _changed_lines(original, config_path.read_text(encoding="utf-8"))
    assert_that(changed).is_length(1)
    assert_that(changed[0][0]).contains(key.rsplit(".", 1)[-1].split("__")[-1])


@pytest.mark.parametrize("name", ["flat two keys", "indent 4 nested", "non-ASCII text"])
def test_windows_line_endings_change_one_line(tmp_path: Path, name: str) -> None:
    """A CRLF file keeps its line ends and changes one line."""
    original, key, value = _CORPUS[name]
    config_path = tmp_path / "c.yaml"
    config_path.write_bytes(original.replace("\n", "\r\n").encode())

    set_config_value(key, value, config_path=config_path)

    after = config_path.read_bytes().decode()
    assert_that(after.count("\r\n")).is_equal_to(after.count("\n"))
    assert_that(_changed_lines(original, after.replace("\r\n", "\n"))).is_length(1)


@pytest.mark.parametrize("name", ["flat two keys", "indent 2 nested", "long string"])
def test_no_final_newline_changes_one_line(tmp_path: Path, name: str) -> None:
    """A file without a final newline still has none and changes one line."""
    original, key, value = _CORPUS[name]
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original.rstrip("\n"), encoding="utf-8")

    set_config_value(key, value, config_path=config_path)

    after = config_path.read_text(encoding="utf-8")
    assert_that(after.endswith("\n")).is_false()
    assert_that(_changed_lines(original, f"{after}\n")).is_length(1)


@pytest.mark.parametrize(("key", "value"), [("workers", 4), ("cache.enabled", False)])
def test_default_generated_config_changes_one_line(
    tmp_path: Path,
    key: str,
    value: object,
) -> None:
    """The config ``config init`` writes takes a set on one line."""
    original = render_config_yaml(WinnowConfig())
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")

    set_config_value(key, value, config_path=config_path)

    changed = _changed_lines(original, config_path.read_text(encoding="utf-8"))
    assert_that(changed).is_length(1)


@pytest.mark.parametrize(
    "original",
    [
        "paths:\n  output_dir: null\n  quarantine_dir:\nworkers: 2\n",
        "paths:\n  output_dir: null\n  quarantine_dir: ~\nworkers: 2\n",
        "paths: {output_dir: }\nworkers: 2\n",
        "extra:\n  -\n  - 1\nworkers: 2\n",
        "workers:    2\ndry_run:    true\n",
        "workers: 2   \ndry_run: true\n",
    ],
)
def test_layout_the_round_trip_changes_is_refused(
    tmp_path: Path,
    original: str,
) -> None:
    """Layouts the round trip cannot keep are refused and left as they are.

    Mixed null spellings, an empty null in a flow map or as a bare ``-``
    item, aligned values and trailing spaces.
    """
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(str(error.value)).contains("without changing its layout")
    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(original)


@pytest.mark.parametrize(
    ("original", "after"),
    [
        ("workers: 2\n", "workers: 2\npaths:\n  output_dir:\n"),
        (
            "workers: 2\nextra: ~\n",
            "workers: 2\nextra: ~\npaths:\n  output_dir: ~\n",
        ),
    ],
)
def test_null_set_by_config_set_uses_the_file_spelling(
    tmp_path: Path,
    original: str,
    after: str,
) -> None:
    """A null is written empty, or as the file already spells nulls."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")

    set_config_value("paths.output_dir", None, config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(after)


@pytest.mark.parametrize("name", list(_EDIT_CORPUS))
def test_edit_keeps_the_lines_around_the_key(tmp_path: Path, name: str) -> None:
    """Comments and blank lines next to the key stay where they were."""
    original, key, value, after = _EDIT_CORPUS[name]
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")

    set_config_value(key, value, config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(after)
