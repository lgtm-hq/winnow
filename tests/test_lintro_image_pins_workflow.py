"""Keep the three lintro image pins identical and in step with the lintro pin.

The lint, security-audit and version-sync workflows each pass the lgtm-ci
reusables a digest-only ``lintro-image``, with the image's lintro version in a
trailing comment. The version-sync check runs ``lintro --version`` inside one
image and compares it with ``pyproject.toml``; it does not look at the other
two digests, the lock or the comments. These tests cover the rest: the three
values are the same image, each comment names the version that
``pyproject.toml`` and ``uv.lock`` pin, and Renovate's regex manager finds
exactly these three lines.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_PINNED_IN = (
    "quality-ci.yml",
    "security-audit.yml",
    "validate-lintro-version.yml",
)
# The only form lgtm-ci's resolve-lintro-image.sh accepts.
_DIGEST_ONLY = re.compile(r"ghcr\.io/lgtm-hq/py-lintro@sha256:[0-9a-f]{64}")
_LINE = re.compile(
    r"^[ \t]*lintro-image:[ \t]*(?P<image>\S+)(?:[ \t]+#[ \t]*(?P<comment>\S+))?"
    r"[ \t]*$",
    re.MULTILINE,
)
_DEPENDENCY = re.compile(r"^lintro==(?P<version>\S+)$")
_IMAGE = "ghcr.io/lgtm-hq/py-lintro@sha256:"
_SAMPLE = _IMAGE + "a" * 64


def _workflow_texts() -> dict[str, str]:
    """Read every workflow file, keyed by file name."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(_WORKFLOWS.glob("*.y*ml"))
    }


def _pins(texts: dict[str, str]) -> dict[str, tuple[str, str | None]]:
    """Return each file's ``lintro-image`` value and its trailing comment.

    A file with more than one ``lintro-image`` line fails the calling test.
    """
    pins: dict[str, tuple[str, str | None]] = {}
    for name, text in texts.items():
        found = [(m.group("image"), m.group("comment")) for m in _LINE.finditer(text)]
        assert_that(len(found)).described_as(name).is_less_than_or_equal_to(1)
        if found:
            pins[name] = found[0]
    return pins


def _yaml_images(text: str) -> list[str]:
    """Return the ``lintro-image`` values GitHub reads from a workflow."""
    workflow = cast(dict[str, Any], yaml.safe_load(text))
    return [
        str(job["with"]["lintro-image"])
        for job in (workflow.get("jobs") or {}).values()
        if "lintro-image" in (job.get("with") or {})
    ]


def _pyproject_version() -> str:
    """Return the version ``pyproject.toml`` pins lintro to."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    versions = [
        match.group("version")
        for spec in project["dependency-groups"]["dev"]
        if (match := _DEPENDENCY.match(spec))
    ]
    assert_that(versions).is_length(1)
    return versions[0]


def _lock_version() -> str:
    """Return the version ``uv.lock`` resolves lintro to."""
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [str(p["version"]) for p in lock["package"] if p["name"] == "lintro"]
    assert_that(versions).is_length(1)
    return versions[0]


def _assert_pins_agree(
    pins: dict[str, tuple[str, str | None]],
    version: str,
) -> None:
    """Require one digest-only image, each line commented with ``version``."""
    for name, (image, comment) in pins.items():
        assert_that(_DIGEST_ONLY.fullmatch(image)).described_as(name).is_not_none()
        assert_that(comment).described_as(f"{name} comment").is_equal_to(version)
    images = {image for image, _ in pins.values()}
    assert_that(images).described_as("lintro images").is_length(1)


def _renovate_pattern() -> re.Pattern[str]:
    """Compile renovate.json's lintro-image match string for Python."""
    config = json.loads((_ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in config["customManagers"]
        if manager.get("depNameTemplate") == "ghcr.io/lgtm-hq/py-lintro"
    ]
    assert_that(managers).is_length(1)
    (match_string,) = managers[0]["matchStrings"]
    return re.compile(str(match_string).replace("(?<", "(?P<"))


def test_the_three_workflows_pin_the_image_once_each() -> None:
    """Guard the collector: exactly these files carry a lintro image."""
    assert_that(sorted(_pins(_workflow_texts()))).is_equal_to(list(_PINNED_IN))


def test_the_lines_read_here_are_the_values_github_reads() -> None:
    """Compare the line scan with the parsed YAML value."""
    texts = _workflow_texts()
    for name, (image, _) in _pins(texts).items():
        assert_that(_yaml_images(texts[name])).described_as(name).is_equal_to(
            [image],
        )


def test_the_images_match_each_other_and_the_lintro_pin() -> None:
    """Keep one image, commented with the version pyproject and the lock pin."""
    version = _pyproject_version()
    assert_that(_lock_version()).is_equal_to(version)
    _assert_pins_agree(_pins(_workflow_texts()), version)


def test_renovate_finds_exactly_the_three_image_pins() -> None:
    """Keep Renovate's regex manager matching each lintro-image line once."""
    pattern = _renovate_pattern()
    texts = _workflow_texts()
    found = {
        name: [(m.group("currentDigest"), m.group("currentValue")) for m in hits]
        for name, text in texts.items()
        if (hits := list(pattern.finditer(text)))
    }
    expected = {
        name: [(image.split("@", 1)[1], comment)]
        for name, (image, comment) in _pins(texts).items()
    }
    assert_that(found).is_equal_to(expected)


@pytest.mark.parametrize(
    "line",
    [
        "    # lintro-image: {image} # 1.2.3",
        "    old-lintro-image: {image} # 1.2.3",
        "    lintro-image: {other_host} # 1.2.3",
        "    lintro-image: {image}",
        "    lintro-image: {image}\n# 1.2.3",
    ],
    ids=["commented-out", "other-key", "unescaped-dot", "no-comment", "next-line"],
)
def test_renovate_ignores_lines_that_are_not_an_image_input(line: str) -> None:
    """Match only a real input line, with its version on the same line."""
    text = line.format(image=_SAMPLE, other_host=_SAMPLE.replace(".", "X", 1))
    assert_that(_renovate_pattern().search(f"jobs:\n{text}\n")).is_none()


def test_renovate_matches_a_real_input_line() -> None:
    """Keep the negative cases above honest with a line that does match."""
    match = _renovate_pattern().search(f"jobs:\n    lintro-image: {_SAMPLE} # 1.2.3\n")
    assert_that(match).is_not_none()
    found = cast(re.Match[str], match)
    assert_that(found.group("currentDigest")).is_equal_to("sha256:" + "a" * 64)
    assert_that(found.group("currentValue")).is_equal_to("1.2.3")


@pytest.mark.parametrize(
    "pins",
    [
        {"a.yml": (_IMAGE + "a" * 64, "1.2.3"), "b.yml": (_IMAGE + "b" * 64, "1.2.3")},
        {"a.yml": (_IMAGE + "a" * 64, "1.2.3"), "b.yml": (_IMAGE + "a" * 64, "1.2.2")},
        {"a.yml": (_IMAGE + "a" * 64, None)},
        {"a.yml": ("ghcr.io/lgtm-hq/py-lintro:1.2.3@sha256:" + "a" * 64, "1.2.3")},
        {"a.yml": ("ghcr.io/lgtm-hq/py-lintro:1.2.3", "1.2.3")},
    ],
    ids=["two-digests", "stale-comment", "no-comment", "tag-and-digest", "tag-only"],
)
def test_pins_that_disagree_fail(pins: dict[str, tuple[str, str | None]]) -> None:
    """Reject a second image, a stale or missing comment, and other forms."""
    with pytest.raises(AssertionError):
        _assert_pins_agree(pins, "1.2.3")


def test_a_second_image_line_in_one_file_fails() -> None:
    """Refuse a file the version check would read only half of."""
    image = _IMAGE + "a" * 64
    text = f"    lintro-image: {image} # 1.2.3\n    lintro-image: {image} # 1.2.3\n"
    with pytest.raises(AssertionError):
        _pins({"a.yml": text})
