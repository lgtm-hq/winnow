"""Characterization of the config file features winnow had through Dynaconf.

Every test above the "Not on main" marker records what ``main`` (Dynaconf
3.3.5) returned and passed against ``main``'s ``winnow/config`` before the
reader was changed (with ``_load_config_data`` read as ``_load_dynaconf_data``
there): the ``.local`` companion file and how it merges, flat ``a.b`` and
``a__b`` keys, ``@`` cast markers in file values, and YAML errors naming the
file. The tests below the marker cover what
``main`` did not do the same way, each saying what it did.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from assertpy import assert_that

from winnow.config import CONFIG_FILE_NAME, load_config, set_config_value
from winnow.config._reader import _load_config_data
from winnow.exceptions import ConfigError

_LOCAL_NAME = ".winnow-config.local.yaml"


def _layers(
    tmp_path: Path,
    files: dict[str, str],
    *,
    target: str = CONFIG_FILE_NAME,
) -> dict[str, object]:
    """Write ``files`` into ``tmp_path`` and read ``target`` without env.

    Args:
        tmp_path: Directory to write into.
        files: File name to content.
        target: The config file to read.

    Returns:
        The raw mapping the reader hands to validation.
    """
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return _load_config_data(tmp_path / target, load_env=False)


def _layers_error(
    tmp_path: Path,
    files: dict[str, str],
    *,
    target: str = CONFIG_FILE_NAME,
) -> ConfigError:
    """Return the ConfigError reading ``files`` raises."""
    with pytest.raises(ConfigError) as raised:
        _layers(tmp_path, files, target=target)
    return raised.value


# --- the .local companion file ------------------------------------------------


def test_companion_overrides_the_config_file(tmp_path: Path) -> None:
    """The reproduced case: the companion's dry_run and workers win."""
    (tmp_path / CONFIG_FILE_NAME).write_text("dry_run: false\nworkers: 2\n")
    (tmp_path / _LOCAL_NAME).write_text("dry_run: true\nworkers: 5\n")

    config = load_config(
        cwd=tmp_path, home_config_dir=tmp_path / "home", load_env=False
    )

    assert_that(config.dry_run).is_true()
    assert_that(config.workers).is_equal_to(5)


@pytest.mark.parametrize(
    ("main", "local", "expected"),
    [
        (
            "workers: 2\n",
            "routing:\n  review: R\n",
            {"routing": {"review": "R"}, "workers": 2},
        ),
        (
            "routing:\n  review: A\n  screenshots: S\n",
            "routing:\n  review: B\n",
            {"routing": {"review": "B", "screenshots": "S"}},
        ),
        (
            "cache:\n  enabled: false\n  directory: /x\n",
            "cache:\n  enabled: true\n",
            {"cache": {"directory": "/x", "enabled": True}},
        ),
        ("source_dirs: [/a]\n", "source_dirs: [/b]\n", {"source_dirs": ["/a", "/b"]}),
        ("source_dirs: [/a]\n", "source_dirs: [/a]\n", {"source_dirs": ["/a"]}),
        (
            "source_dirs: [/a, /b]\n",
            "source_dirs: [/b]\n",
            {"source_dirs": ["/a", "/b", "/b"]},
        ),
        ("source_dirs: [/a]\n", "source_dirs: []\n", {"source_dirs": ["/a"]}),
        (
            "paths:\n  include_patterns: ['*.a']\n",
            "paths:\n  include_patterns: ['*.b']\n",
            {"paths": {"include_patterns": ["*.a", "*.b"]}},
        ),
        (
            "routing:\n  review: A\n",
            "routing: {}\n",
            {"routing": {"review": "A"}},
        ),
        ("workers: 2\n", "workers: null\n", {"workers": None}),
        (
            "organize:\n  max_depth: 3\n",
            "organize:\n  max_depth: null\n",
            {"organize": {"max_depth": None}},
        ),
        ("routing:\n  review: A\n", "routing: 5\n", {"routing": 5}),
        ("workers: 2\n", "workers:\n  a: 1\n", {"workers": {"a": 1}}),
        ("workers: 2\n", "", {"workers": 2}),
        ("workers: 2\n", "# only a comment\n", {"workers": 2}),
        ("", "workers: 4\n", {"workers": 4}),
        (
            "Routing:\n  Review: A\n",
            "routing:\n  review: B\n",
            {"routing": {"review": "B"}},
        ),
        (
            "routing:\n  review: A\n",
            "ROUTING:\n  REVIEW: B\n  screenshots: S\n",
            {"routing": {"review": "B", "screenshots": "S"}},
        ),
        (
            "routing:\n  review: A\n",
            "routing.screenshots: S\n",
            {"routing": {"review": "A", "screenshots": "S"}},
        ),
        (
            "routing:\n  review: A\n",
            "routing__review: B\n",
            {"routing": {"review": "B"}},
        ),
        ("workers: 2\n", "workers: '@int 6'\n", {"workers": 6}),
        (
            "paths:\n  include_patterns: [a]\n",
            "paths.include_patterns: [b]\n",
            {"paths": {"include_patterns": ["a", "b"]}},
        ),
    ],
    ids=[
        "adds-keys",
        "nested-merge",
        "deep-merge",
        "lists-concatenate",
        "equal-lists-once",
        "overlapping-lists-keep-duplicates",
        "empty-list-keeps-old",
        "nested-lists-concatenate",
        "empty-mapping-keeps-old",
        "null-replaces",
        "nested-null-replaces",
        "scalar-replaces-mapping",
        "mapping-replaces-scalar",
        "empty-companion",
        "comment-only-companion",
        "empty-config-file",
        "keys-match-case-insensitively",
        "upper-case-companion-keys",
        "dotted-key-in-companion",
        "dunder-key-in-companion",
        "marker-in-companion",
        "dotted-list-in-companion",
    ],
)
def test_companion_merges_like_dynaconf(
    tmp_path: Path,
    main: str,
    local: str,
    expected: dict[str, object],
) -> None:
    """The companion merges into the config file with Dynaconf's rules."""
    data = _layers(tmp_path, {CONFIG_FILE_NAME: main, _LOCAL_NAME: local})

    assert_that(data).is_equal_to(expected)


@pytest.mark.parametrize(
    ("files", "target", "expected"),
    [
        ({"my.yaml": "workers: 2\n", "my.local.yaml": "workers: 7\n"}, "my.yaml", 7),
        ({"my.yml": "workers: 2\n", "my.local.yml": "workers: 7\n"}, "my.yml", 7),
        ({"x.local.yaml": "workers: 3\n"}, "x.local.yaml", 3),
        ({"c.toml": "workers = 4\n", "c.local.toml": "workers = 6\n"}, "c.toml", 6),
        ({"c.json": '{"workers": 4}', "c.local.json": '{"workers": 6}'}, "c.json", 6),
        ({"c.yaml": "workers: 4\n", "c.local.toml": "workers = 6\n"}, "c.yaml", 4),
        ({"c.tml": "workers = 4\n"}, "c.tml", 4),
    ],
    ids=[
        "explicit-name",
        "yml",
        "local-file-has-no-companion",
        "toml",
        "json",
        "companion-needs-same-extension",
        "tml",
    ],
)
def test_companion_name_and_formats(
    tmp_path: Path,
    files: dict[str, str],
    target: str,
    expected: int,
) -> None:
    """The companion is ``<name>.local.<ext>``; TOML and JSON files are read."""
    data = _layers(tmp_path, files, target=target)

    assert_that(data).is_equal_to({"workers": expected})


def test_companion_directory_is_ignored(tmp_path: Path) -> None:
    """A directory named like the companion is not a companion."""
    (tmp_path / _LOCAL_NAME).mkdir()

    data = _layers(tmp_path, {CONFIG_FILE_NAME: "workers: 2\n"})

    assert_that(data).is_equal_to({"workers": 2})


def test_invalid_companion_is_a_config_error_naming_it(tmp_path: Path) -> None:
    """A companion that is not valid YAML fails the load and is named."""
    error = _layers_error(
        tmp_path,
        {CONFIG_FILE_NAME: "workers: 2\n", _LOCAL_NAME: "workers: [\n"},
    )

    assert_that(error.context.operation).is_equal_to("load_config")
    assert_that(str(error.context.details["error"])).contains(
        str(tmp_path / _LOCAL_NAME)
    )


# --- flat keys ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("routing.screenshots: Shots\n", {"routing": {"screenshots": "Shots"}}),
        ("routing__review: Rev\n", {"routing": {"review": "Rev"}}),
        ("ROUTING__REVIEW: Rev\n", {"routing": {"review": "Rev"}}),
        ("ROUTING__Review: R\n", {"routing": {"review": "R"}}),
        ("Routing.Review: X\n", {"routing": {"review": "X"}}),
        (
            "routing:\n  review: A\nrouting.screenshots: Shots\n",
            {"routing": {"review": "A", "screenshots": "Shots"}},
        ),
        (
            "routing.screenshots: X\nrouting:\n  review: A\n",
            {"routing": {"review": "A", "screenshots": "X"}},
        ),
        ("routing.review: X\nrouting:\n  review: A\n", {"routing": {"review": "A"}}),
        ("routing:\n  review: A\nrouting.review: X\n", {"routing": {"review": "X"}}),
        ("routing__review: X\nrouting.review: Y\n", {"routing": {"review": "Y"}}),
        ("routing.review: A\nRouting.review: B\n", {"routing": {"review": "B"}}),
        (
            "cache__enabled: false\ncache__directory: /z\n",
            {"cache": {"directory": "/z", "enabled": False}},
        ),
        (
            "cache:\n  enabled: false\ncache__directory: /d\n",
            {"cache": {"directory": "/d", "enabled": False}},
        ),
        ("cache.x.y: 1\n", {"cache": {"x": {"y": 1}}}),
        ("workers.x: 1\n", {"workers": {"x": 1}}),
        ("workers: 2\nworkers.x: 1\n", {"workers": {"x": 1}}),
        ("source_dirs: [/a]\nsource_dirs.x: 1\n", {"source_dirs": {"x": 1}}),
        ("routing:\n  a.b: 1\n", {"routing": {"a.b": 1}}),
        ("routing:\n  a__b: 1\n", {"routing": {"a__b": 1}}),
        ("' routing.review ': X\n", {"routing": {"review ": "X"}}),
        ("'routing . review': X\n", {"routing": {" review": "X"}}),
        ("' workers ': 3\n", {"workers": 3}),
        ("routing.: X\nworkers: 1\n", {"routing": {"": "X"}, "workers": 1}),
        (
            "routing..review: X\nworkers: 1\n",
            {"routing": {"": {"review": "X"}}, "workers": 1},
        ),
        (".routing: X\nworkers: 1\n", {"workers": 1}),
        ("a__b__c: 1\nworkers: 1\n", {"workers": 1}),
        (
            "paths:\n  include_patterns: [a]\npaths.include_patterns: [b]\n",
            {"paths": {"include_patterns": ["a", "b"]}},
        ),
        (
            "paths.include_patterns: [a]\npaths__include_patterns: [b]\n",
            {"paths": {"include_patterns": ["a", "b"]}},
        ),
        (
            "paths:\n  output_dir: /o\npaths.include_patterns: [x]\n",
            {"paths": {"include_patterns": ["x"], "output_dir": "/o"}},
        ),
        ("routing.review: '@str 1'\n", {"routing": {"review": "1"}}),
        ("organize__max_depth: '@int 4'\n", {"organize": {"max_depth": 4}}),
        (
            "workers: 1\nrouting:\n  review: A\nWORKERS: 3\n",
            {"routing": {"review": "A"}, "workers": 3},
        ),
        (
            "Routing:\n  review: A\nrouting:\n  screenshots: B\n",
            {"routing": {"screenshots": "B"}},
        ),
        ("source_dirs[0]: /a\nworkers: 1\n", {"workers": 1}),
    ],
)
def test_flat_keys_load_as_nested_settings(
    tmp_path: Path,
    text: str,
    expected: dict[str, object],
) -> None:
    """Top-level ``a.b`` and ``a__b`` keys set nested settings, as on main."""
    assert_that(_layers(tmp_path, {CONFIG_FILE_NAME: text})).is_equal_to(expected)


# --- markers in file values -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("workers: '@int 4'\n", {"workers": 4}),
        ("workers: '@int  4 '\n", {"workers": 4}),
        ("workers: '@int 1_0'\n", {"workers": 10}),
        ("min_similarity: '@float 0.5'\n", {"min_similarity": 0.5}),
        ("min_similarity: '@float 1'\n", {"min_similarity": 1.0}),
        ("dry_run: '@bool false'\n", {"dry_run": False}),
        (
            "dry_run: '@bool yes'\nrecursive: '@bool nope'\n",
            {"dry_run": True, "recursive": False},
        ),
        ("source_dirs: '@json [\"/a\"]'\n", {"source_dirs": ["/a"]}),
        ("source_dirs: \"@json ['/a']\"\n", {"source_dirs": ["/a"]}),
        (
            "routing: \"@json {'review': 'R', 'enabled': True}\"\n",
            {"routing": {"enabled": True, "review": "R"}},
        ),
        ("routing:\n  review: '@str 12'\n", {"routing": {"review": "12"}}),
        ("organize:\n  max_depth: '@none x'\n", {"organize": {"max_depth": None}}),
        ("organize:\n  max_depth: '@null'\n", {"organize": {"max_depth": None}}),
        ("workers: '@note hi'\n", {"workers": None}),
        ("routing:\n  review: '@upper rev'\n", {"routing": {"review": "REV"}}),
        ("source_dirs: ['@str /a', '@int 3']\n", {"source_dirs": ["/a", 3]}),
        ("source_dirs: ['@str /a', '@none']\n", {"source_dirs": ["/a", None]}),
        ("cache:\n  enabled: '@bool off'\n", {"cache": {"enabled": False}}),
        ("workers: '@INT 4'\n", {"workers": "@INT 4"}),
        ('workers: "@int\\t4"\n', {"workers": "@int\t4"}),
        ("routing:\n  review: '[1, 2]'\n", {"routing": {"review": "[1, 2]"}}),
        ("workers: !!int '4'\n", {"workers": 4}),
    ],
)
def test_file_value_markers_are_cast(
    tmp_path: Path,
    text: str,
    expected: dict[str, object],
) -> None:
    """Cast markers in config file values are applied as on main."""
    assert_that(_layers(tmp_path, {CONFIG_FILE_NAME: text})).is_equal_to(expected)


@pytest.mark.parametrize(
    ("files", "target"),
    [
        ({"c.toml": 'workers = "@int 7"\n'}, "c.toml"),
        ({"c.toml": '"routing.review" = "X"\nworkers = 7\n'}, "c.toml"),
    ],
    ids=["toml-marker", "toml-dotted-key"],
)
def test_toml_files_get_markers_and_flat_keys(
    tmp_path: Path,
    files: dict[str, str],
    target: str,
) -> None:
    """TOML config files get the same marker and flat-key handling."""
    data = _layers(tmp_path, files, target=target)

    assert_that(data).contains_entry({"workers": 7})


# --- YAML errors name the file ------------------------------------------------


def test_yaml_error_names_the_config_file(tmp_path: Path) -> None:
    """A YAML syntax error names the config file, not ``<unicode string>``."""
    error = _layers_error(tmp_path, {CONFIG_FILE_NAME: "workers: [\n"})

    message = str(error.context.details["error"])
    assert_that(message).contains(str(tmp_path / CONFIG_FILE_NAME))
    assert_that(message).does_not_contain("<unicode string>")


# --- Not on main: differences, each with what main did ----------------------


def test_config_set_keeps_companion_values_out_of_the_file(tmp_path: Path) -> None:
    """``config set`` changes only its key (#429).

    On main it wrote the merged settings, so ``dry_run: true`` from the
    companion landed in the file.
    """
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\ndry_run: false\n")
    (tmp_path / _LOCAL_NAME).write_text("dry_run: true\nworkers: 5\n")

    set_config_value("workers", "3", cwd=tmp_path, home_config_dir=tmp_path / "home")

    written = yaml.safe_load((tmp_path / CONFIG_FILE_NAME).read_text())
    assert_that(written).is_equal_to({"workers": "3", "dry_run": False})
    assert_that((tmp_path / _LOCAL_NAME).read_text()).is_equal_to(
        "dry_run: true\nworkers: 5\n",
    )


def test_flat_keys_survive_config_set(tmp_path: Path) -> None:
    """``config set`` keeps flat keys as written (#429).

    On main it rewrote them as nested ones.
    """
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "routing.screenshots: Shots\nrouting__review: Rev\nworkers: 2\n",
    )

    set_config_value("workers", "3", cwd=tmp_path, home_config_dir=tmp_path / "home")

    assert_that((tmp_path / CONFIG_FILE_NAME).read_text()).is_equal_to(
        "routing.screenshots: Shots\nrouting__review: Rev\nworkers: '3'\n",
    )
    config = load_config(
        cwd=tmp_path, home_config_dir=tmp_path / "home", load_env=False
    )
    assert_that(config.routing.screenshots).is_equal_to("Shots")
    assert_that(config.routing.review).is_equal_to("Rev")
    assert_that(config.workers).is_equal_to(3)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("workers: 2\ndynaconf_include: [inc.yaml]\n", "dynaconf_include"),
        ("workers: 2\nincludes_for_dynaconf: [inc.yaml]\n", "includes_for_dynaconf"),
        ("workers: 2\nINCLUDES_FOR_DYNACONF: [inc.yaml]\n", "INCLUDES_FOR_DYNACONF"),
        (
            "workers: 2\nmerge_enabled_for_dynaconf: false\n",
            "merge_enabled_for_dynaconf",
        ),
        ("dynaconf_merge: true\nworkers: 2\n", "dynaconf_merge"),
        ("dynaconf_dotted_lookup: false\nworkers: 2\n", "dynaconf_dotted_lookup"),
    ],
)
def test_dynaconf_file_settings_are_errors(
    tmp_path: Path,
    text: str,
    key: str,
) -> None:
    """Dynaconf loading settings in a file raise ``ConfigError`` naming them.

    main applied them: ``dynaconf_include`` and ``includes_for_dynaconf`` read
    the listed files (``workers`` became 9 from ``inc.yaml``), and the others
    changed how later files merged or how keys were split.
    """
    (tmp_path / "inc.yaml").write_text("workers: 9\n")

    error = _layers_error(tmp_path, {CONFIG_FILE_NAME: text})

    assert_that(error.message).contains(repr(key))
    assert_that(error.context.operation).is_equal_to("load_config")


@pytest.mark.parametrize(
    ("local", "token"),
    [
        ("source_dirs: [dynaconf_merge, /b]\n", "dynaconf_merge"),
        ("source_dirs: [/b, dynaconf_merge_unique]\n", "dynaconf_merge_unique"),
        ("routing:\n  dynaconf_merge: false\n  screenshots: X\n", "dynaconf_merge"),
    ],
)
def test_dynaconf_merge_tokens_are_errors(
    tmp_path: Path,
    local: str,
    token: str,
) -> None:
    """Merge tokens raise ``ConfigError`` naming them.

    main applied them in the companion (``dynaconf_merge: false`` replaced the
    whole mapping) and kept them as data in the config file.
    """
    error = _layers_error(
        tmp_path,
        {
            CONFIG_FILE_NAME: "source_dirs: [/a]\nrouting:\n  review: A\n",
            _LOCAL_NAME: local,
        },
    )

    assert_that(error.message).contains(repr(token))


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("workers: '@del'\n", "@del"),
        ("routing:\n  review: '@format {this.workers}'\n", "@format"),
        ("routing:\n  review: '@jinja {{this.workers}}'\n", "@jinja"),
        ("routing:\n  review: '@get workers'\n", "@get"),
        ("routing:\n  review: '@read_file r.txt'\n", "@read_file"),
        ("source_dirs: '@merge /a,/b'\n", "@merge"),
        ("workers: '@empty'\n", "@empty"),
        ("workers: '@int @format {this.x}'\nx: 5\n", "@format"),
    ],
)
def test_dynaconf_only_file_markers_are_errors(
    tmp_path: Path,
    text: str,
    marker: str,
) -> None:
    """Markers that need Dynaconf raise ``ConfigError`` naming the marker.

    main evaluated them (``@format`` and ``@get`` read other settings,
    ``@read_file`` read a file, ``@del`` removed the key, ``@empty`` gave the
    text ``EMPTY``) or failed with ``ImportError`` (``@jinja``).
    """
    error = _layers_error(tmp_path, {CONFIG_FILE_NAME: text})

    assert_that(str(error.context.details["error"])).contains(f"the {marker} marker")


@pytest.mark.parametrize(
    "text",
    ["workers: '@int abc'\n", "workers: '@int'\n", "workers: '@json {bad'\n"],
)
def test_uncastable_file_marker_is_a_config_error(tmp_path: Path, text: str) -> None:
    """An uncastable marker value is a ``ConfigError``; main leaked Dynaconf's."""
    error = _layers_error(tmp_path, {CONFIG_FILE_NAME: text})

    assert_that(error.context.operation).is_equal_to("load_config")


@pytest.mark.parametrize(
    ("name", "kind"),
    [("c.ini", "INI"), ("c.conf", "INI"), ("c.properties", "INI"), ("c.py", "Python")],
)
def test_ini_and_python_config_files_are_errors(
    tmp_path: Path,
    name: str,
    kind: str,
) -> None:
    """INI and Python config files raise ``ConfigError``.

    main ignored INI files without a word (its INI extra was not installed)
    and executed a Python config file.
    """
    error = _layers_error(tmp_path, {name: "workers = 4\n"}, target=name)

    assert_that(error.message).contains(kind)


@pytest.mark.parametrize("name", ["cfg", "cfg.txt", "c.YAML"])
def test_other_extensions_are_read_as_yaml(tmp_path: Path, name: str) -> None:
    """A config file with another extension is read as YAML.

    main read nothing from such a file and returned the defaults.
    """
    data = _layers(tmp_path, {name: "workers: 4\n"}, target=name)

    assert_that(data).is_equal_to({"workers": 4})


@pytest.mark.parametrize(
    ("local", "fragment"),
    [
        ("- 1\n", "must be a mapping"),
        ("workers: 1\nworkers: 3\n", "duplicate key"),
    ],
    ids=["not-a-mapping", "duplicate-key"],
)
def test_bad_companion_is_a_config_error(
    tmp_path: Path,
    local: str,
    fragment: str,
) -> None:
    """A companion that is not a mapping, or repeats a key, is a ``ConfigError``.

    main leaked ``AttributeError`` and ruamel's ``DuplicateKeyError``.
    """
    error = _layers_error(
        tmp_path, {CONFIG_FILE_NAME: "workers: 2\n", _LOCAL_NAME: local}
    )

    assert_that(str(error.context.details["error"])).contains(fragment)


@pytest.mark.parametrize(
    ("name", "text"),
    [("c.json", "[1]"), ("c.toml", "workers = \n"), ("c.json", "{")],
    ids=["json-not-a-mapping", "invalid-toml", "invalid-json"],
)
def test_bad_toml_and_json_files_are_config_errors(
    tmp_path: Path,
    name: str,
    text: str,
) -> None:
    """Invalid TOML or JSON, or a JSON array, is a ``ConfigError``."""
    error = _layers_error(tmp_path, {name: text}, target=name)

    assert_that(error.context.operation).is_equal_to("load_config")


def test_dynaconf_environment_variables_are_named_in_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dynaconf's own variables are ignored with a warning naming them.

    On main ``INCLUDES_FOR_DYNACONF=inc.yaml`` loaded that file into winnow's
    settings (``workers`` became 9), and ``SETTINGS_FILE_FOR_DYNACONF``,
    ``SECRETS_FOR_DYNACONF`` or ``ENVVAR_PREFIX_FOR_DYNACONF`` did similar.
    """
    (tmp_path / "inc.yaml").write_text("workers: 9\n")
    monkeypatch.setenv("INCLUDES_FOR_DYNACONF", str(tmp_path / "inc.yaml"))

    with caplog.at_level(logging.WARNING):
        data = _layers(tmp_path, {CONFIG_FILE_NAME: "workers: 2\n"})

    assert_that(data).is_equal_to({"workers": 2})
    assert_that(caplog.text).contains("INCLUDES_FOR_DYNACONF")
