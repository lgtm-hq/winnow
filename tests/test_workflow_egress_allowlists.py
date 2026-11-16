"""Keep the workflows' own network allow-lists usable.

Some callers pass a full ``allowed-endpoints`` list instead of an lgtm-ci
preset. A hand-copied list drifts: an entry that no longer resolves stops the
hardening agent at start, which fails the job closed, and a host the preset
gained later is blocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# Hosts that do not exist. The hardening agent resolves every allowed domain
# when it starts and exits if one cannot be resolved, so a single dead entry
# fails the whole job before any step of the project runs. TestPyPI uploads go
# to test.pypi.org; lgtm-ci removed this name from its presets
# (lgtm-hq/lgtm-ci#1206), and it blocked the v0.37.8 release here (#419).
_UNRESOLVABLE = {"upload.test.pypi.org:443"}
# Hosts of lgtm-ci's ``docker`` preset that the Docker caller's own list must
# carry. The first is where Docker Hub serves some image layers from; without
# it the BuildKit image pull times out on a blocked connection (#419).
_DOCKER_REQUIRED = {
    "docker-images-prod.s3.dualstack.us-east-1.amazonaws.com:443",
    "fulcio.githubapp.com:443",
    "timestamp.githubapp.com:443",
}


def _allow_lists(node: Any, where: str = "") -> list[tuple[str, set[str]]]:
    """Collect every ``allowed-endpoints`` list in a parsed workflow."""
    found: list[tuple[str, set[str]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "allowed-endpoints" and isinstance(value, str):
                found.append((where, set(value.split())))
            else:
                found.extend(_allow_lists(value, f"{where}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_allow_lists(value, f"{where}[{index}]"))
    return found


def _all_allow_lists() -> list[tuple[str, set[str]]]:
    """Collect the allow-lists of every checked-in workflow."""
    found: list[tuple[str, set[str]]] = []
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        found.extend(_allow_lists(workflow, path.name))
    return found


def test_workflows_carry_their_own_allow_lists() -> None:
    """Guard the collector: these tests must not pass on nothing."""
    names = {where.split("/")[0] for where, _ in _all_allow_lists()}
    assert_that(names).contains(
        "docker-build-publish.yml",
        "publish-pypi-on-tag.yml",
        "publish-testpypi.yml",
    )


def test_no_allow_list_names_a_host_that_does_not_resolve() -> None:
    """One dead entry stops the hardening agent and fails the job."""
    for where, hosts in _all_allow_lists():
        assert_that(hosts & _UNRESOLVABLE).described_as(where).is_empty()


def test_docker_allow_list_has_the_preset_hosts_it_replaces() -> None:
    """The Docker caller replaces the preset, so it must carry these hosts."""
    lists = [
        hosts
        for where, hosts in _all_allow_lists()
        if where.startswith("docker-build-publish.yml")
    ]
    assert_that(lists).is_not_empty()
    for hosts in lists:
        assert_that(_DOCKER_REQUIRED - hosts).is_empty()


def test_the_collector_finds_nested_lists() -> None:
    """Find lists on reusable calls and on steps alike."""
    workflow = {
        "jobs": {
            "a": {"with": {"allowed-endpoints": "one:443 two:443"}},
            "b": {"steps": [{"with": {"allowed-endpoints": "three:443\nfour:443\n"}}]},
        },
    }
    found = dict(_allow_lists(workflow, "x.yml"))
    assert_that(found).is_equal_to(
        {
            "x.yml/jobs/a/with": {"one:443", "two:443"},
            "x.yml/jobs/b/steps[0]/with": {"three:443", "four:443"},
        },
    )
