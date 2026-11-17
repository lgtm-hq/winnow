"""tomli stays below 2.4, and the env corpus is what notices if it does not.

winnow casts ``WINNOW_*`` values with tomli's TOML 1.0 grammar. tomli 2.4 and
later read TOML 1.1, which casts some values differently (``12:30`` becomes a
time, ``"\\e"`` an escape character). The cap in ``pyproject.toml`` keeps the
grammar; the Renovate rule keeps a dependency update from lifting the cap; and
``tests/config/env_corpus.json`` holds the values whose result would change, so
``test_env_corpus.py`` fails if a TOML 1.1 tomli is ever installed.
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib.metadata import version
from pathlib import Path

from assertpy import assert_that

_ROOT = Path(__file__).resolve().parents[2]
_CAP = (2, 4)

_TOML_11_SENSITIVE: dict[str, str] = {
    "12:30": "12:30",
    "[12:30]": "[12:30]",
    '"\\e"': '"\\e"',
    '"\\x41"': '"\\x41"',
    "{a=1,}": "{a=1,}",
}
"""Raw values TOML 1.1 casts (to a time, a list, a string, a table) that TOML
1.0 rejects, so winnow keeps them as the text given."""


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _tomli_requirement() -> str:
    """Return the tomli requirement declared in ``pyproject.toml``."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        requirement
        for requirement in project["project"]["dependencies"]
        if re.match(r"tomli\b", requirement)
    ]
    assert_that(requirements).is_length(1)
    return str(requirements[0])


def test_declared_tomli_range_excludes_2_4() -> None:
    """``pyproject.toml`` caps tomli strictly below 2.4."""
    clauses = [
        clause.strip()
        for clause in _tomli_requirement().removeprefix("tomli").split(",")
    ]
    upper = [_version_tuple(clause) for clause in clauses if clause.startswith("<")]

    assert_that(upper).is_not_empty()
    assert_that(min(upper) <= _CAP).described_as(str(min(upper))).is_true()
    assert_that([clause for clause in clauses if clause.startswith("<=")]).is_empty()


def test_installed_tomli_is_below_2_4() -> None:
    """The tomli in this environment is a TOML 1.0 release."""
    installed = version("tomli")

    assert_that(_version_tuple(installed) < _CAP).described_as(installed).is_true()


def test_renovate_keeps_tomli_below_2_4() -> None:
    """A Renovate rule holds tomli updates below 2.4 and says why."""
    config = json.loads((_ROOT / "renovate.json").read_text(encoding="utf-8"))
    rules = [
        rule
        for rule in config.get("packageRules", [])
        if rule.get("matchPackageNames") == ["tomli"]
    ]

    assert_that(rules).is_length(1)
    assert_that(rules[0]["allowedVersions"]).is_equal_to("<2.4")
    assert_that(rules[0]["description"]).contains("TOML 1.1")


def test_env_corpus_holds_the_values_toml_1_1_changes() -> None:
    """The corpus records TOML 1.0's reading of values TOML 1.1 reads otherwise.

    These are the rows ``test_env_corpus.py`` would fail on with tomli 2.4 or
    later installed, so the corpus, not the cap alone, protects the grammar.
    """
    corpus = json.loads(
        (_ROOT / "tests" / "config" / "env_corpus.json").read_text(encoding="utf-8"),
    )
    recorded = {entry["raw"]: entry.get("winnow", entry["main"]) for entry in corpus}

    for raw, text in _TOML_11_SENSITIVE.items():
        assert_that(recorded).described_as(raw).contains_entry({raw: ["str", text]})
