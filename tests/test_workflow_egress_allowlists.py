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
# Names known not to exist, compared without the port. The hardening agent
# resolves every allowed domain when it starts and exits if one cannot be
# resolved. Where lgtm-ci verifies the agent, the job then fails before any
# step of the project runs; where a job starts the agent itself, it carries on
# with no egress enforcement at all, which is worse. This is a list of the
# names that have bitten, not a resolution check: the tests stay offline.
#   upload.test.pypi.org          TestPyPI uploads go to test.pypi.org; lgtm-ci
#                                 dropped it (lgtm-hq/lgtm-ci#1206); it blocked
#                                 the v0.37.8 release (#419).
#   actions.githubusercontent.com only the pipelines. and token. subdomains
#                                 exist; with it, the v0.37.7 smoke test,
#                                 upload and Homebrew jobs ran unenforced.
_KNOWN_DEAD_NAMES = {"upload.test.pypi.org", "actions.githubusercontent.com"}
# Hosts of lgtm-ci's ``docker`` preset that the Docker caller's own list must
# carry. The first is where Docker Hub serves some image layers from; without
# it the BuildKit image pull times out on a blocked connection (#419).
_DOCKER_REQUIRED = {
    "docker-images-prod.s3.dualstack.us-east-1.amazonaws.com:443",
    "fulcio.githubapp.com:443",
    "timestamp.githubapp.com:443",
}


def _hosts(value: Any, where: str) -> set[str]:
    """Read one ``allowed-endpoints`` value: a string or a list of strings.

    Anything else raises, so a list written in a form these tests cannot
    read fails them instead of being skipped.
    """
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return {host for item in value for host in item.split()}
    msg = f"{where}: allowed-endpoints must be a string or a list of strings"
    raise TypeError(msg)


def _allow_lists(node: Any, where: str = "") -> list[tuple[str, set[str]]]:
    """Collect every ``allowed-endpoints`` list in a parsed workflow."""
    found: list[tuple[str, set[str]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "allowed-endpoints":
                found.append((where, _hosts(value, where)))
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


def _names(hosts: set[str]) -> set[str]:
    """Strip the port from each ``host:port`` entry."""
    return {host.rsplit(":", 1)[0] if ":" in host else host for host in hosts}


def test_no_allow_list_names_a_known_dead_host() -> None:
    """One dead entry stops the hardening agent, whatever port it names."""
    for where, hosts in _all_allow_lists():
        assert_that(_names(hosts) & _KNOWN_DEAD_NAMES).described_as(where).is_empty()


def test_dead_names_are_matched_without_the_port() -> None:
    """Catch a dead name on another port or with none; keep its subdomains."""
    hosts = {
        "upload.test.pypi.org:80",
        "actions.githubusercontent.com",
        "pipelines.actions.githubusercontent.com:443",
        "token.actions.githubusercontent.com:443",
        "test.pypi.org:443",
    }
    assert_that(_names(hosts) & _KNOWN_DEAD_NAMES).is_equal_to(
        {"upload.test.pypi.org", "actions.githubusercontent.com"},
    )


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


def test_the_collector_reads_a_list_and_refuses_what_it_cannot_read() -> None:
    """Read a YAML sequence too, and fail on any other form."""
    found = dict(
        _allow_lists({"with": {"allowed-endpoints": ["one:443", "two:443 three:443"]}})
    )
    assert_that(found).is_equal_to({"/with": {"one:443", "two:443", "three:443"}})
    for unreadable in ({"one:443": None}, 443, None, ["one:443", 2]):
        assert_that(_allow_lists).raises(TypeError).when_called_with(
            {"with": {"allowed-endpoints": unreadable}},
        )
