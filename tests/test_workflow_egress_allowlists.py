"""Keep the workflows' own network allow-lists usable.

A hand-copied ``allowed-endpoints`` list drifts: an entry that no longer
resolves stops the hardening agent at start, which fails the job closed, and a
host the preset gained later is blocked. So an lgtm-ci reusable call takes its
reusable's preset and appends only the hosts winnow needs (#465). A call that
still replaces the preset with a full list is a listed exception with a reason.

The job's allow-list is then the preset's hosts plus the appended ones, so the
hosts a caller depends on are checked against that combined set, using the
preset hosts read from lgtm-ci at the pinned commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_REUSABLE = re.compile(
    r"^lgtm-hq/lgtm-ci/\.github/workflows/(?P<name>[^@]+)@(?P<pin>\S+)",
    re.IGNORECASE,
)
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
# Names that resolve but that no job should list by hand any more (#465).
#   test.pypi.org      TestPyPI; winnow no longer publishes there (#357).
#                      lgtm-ci's python-dist preset still carries it.
#   sigstore-tuf-root.storage.googleapis.com
#                      legacy Sigstore TUF root; lgtm-ci v0.112.0 dropped it
#                      and no tag build from v0.37.15 to v0.37.19 called it.
_RETIRED_NAMES = {"test.pypi.org", "sigstore-tuf-root.storage.googleapis.com"}

# The lgtm-ci commit the preset hosts below were read from: v0.116.3. Every
# call must pin it, so moving the pin means reading the hosts again.
_LGTM_CI_COMMIT = "0c373f1252e0fe23a1e119df639b51d63b02b7d8"
# Hosts of the presets winnow's reusable calls name, printed by
# ``egress_preset_endpoints <preset>`` from lgtm-ci's
# scripts/ci/lib/egress/presets.sh at ``_LGTM_CI_COMMIT``. None of these
# presets adds GitHub's results storage hosts.
_PRESET_HOSTS: dict[str, frozenset[str]] = {
    "docker": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "objects.githubusercontent.com:443",
            "raw.githubusercontent.com:443",
            "release-assets.githubusercontent.com:443",
            "github-releases.githubusercontent.com:443",
            "pipelines.actions.githubusercontent.com:443",
            "token.actions.githubusercontent.com:443",
            "ghcr.io:443",
            "pkg-containers.githubusercontent.com:443",
            "docker.io:443",
            "registry-1.docker.io:443",
            "auth.docker.io:443",
            "production.cloudflare.docker.com:443",
            "production.cloudfront.docker.com:443",
            "docker-images-prod.s3.dualstack.us-east-1.amazonaws.com:443",
            "mirror.gcr.io:443",
            "fulcio.sigstore.dev:443",
            "rekor.sigstore.dev:443",
            "timestamp.sigstore.dev:443",
            "tuf-repo-cdn.sigstore.dev:443",
            "fulcio.githubapp.com:443",
            "timestamp.githubapp.com:443",
        },
    ),
    "github-minimal": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "objects.githubusercontent.com:443",
            "pipelines.actions.githubusercontent.com:443",
        },
    ),
    "github-tooling": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "objects.githubusercontent.com:443",
            "raw.githubusercontent.com:443",
            "release-assets.githubusercontent.com:443",
            "uploads.github.com:443",
            "pipelines.actions.githubusercontent.com:443",
        },
    ),
    "python-dist": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "release-assets.githubusercontent.com:443",
            "objects.githubusercontent.com:443",
            "raw.githubusercontent.com:443",
            "pipelines.actions.githubusercontent.com:443",
            "pypi.org:443",
            "files.pythonhosted.org:443",
            "releases.astral.sh:443",
            "test.pypi.org:443",
            "upload.pypi.org:443",
            "fulcio.sigstore.dev:443",
            "rekor.sigstore.dev:443",
            "timestamp.sigstore.dev:443",
            "tuf-repo-cdn.sigstore.dev:443",
            "oauth2.sigstore.dev:443",
        },
    ),
    "scorecard": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "objects.githubusercontent.com:443",
            "raw.githubusercontent.com:443",
            "pipelines.actions.githubusercontent.com:443",
            "gcr.io:443",
            "api.osv.dev:443",
            "api.scorecard.dev:443",
            "api.securityscorecards.dev:443",
        },
    ),
}

# Reusable calls that still replace their preset with a full list, and why.
# The preset each would get instead adds hosts the job does not use today;
# #465 allows no call to gain a host.
_REPLACEMENT_LISTS = {
    "ghcr-cleanup.yml/prune-untagged": (
        "github-tooling plus ghcr.io and pkg-containers.githubusercontent.com"
        " would add release-assets.githubusercontent.com"
    ),
    "dependency-review.yml/dependency-review": (
        "github-tooling, its only accepted preset, would add uploads.github.com"
    ),
}

# Hosts each call that uses its preset must still be able to reach, wherever
# they come from (the preset or the appended list). Each set holds the
# allow-listed hosts the job called in the runs named, read from
# harden-runner's "endpoint called" lines, plus any host named with its reason.
_REQUIRED_HOSTS = {
    # Main run 45247360714 and v0.37.19 tag run 45228316468. Also, as before
    # #465: Docker Hub serves some image layers from the S3 host, and without
    # it the BuildKit pull times out on a blocked connection (#419); the two
    # githubapp.com hosts are GitHub's own Sigstore instance for attestations.
    "docker-build-publish.yml/docker": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "release-assets.githubusercontent.com:443",
            "token.actions.githubusercontent.com:443",
            "ghcr.io:443",
            "pkg-containers.githubusercontent.com:443",
            "registry-1.docker.io:443",
            "auth.docker.io:443",
            "production.cloudfront.docker.com:443",
            "docker-images-prod.s3.dualstack.us-east-1.amazonaws.com:443",
            "fulcio.sigstore.dev:443",
            "rekor.sigstore.dev:443",
            "timestamp.sigstore.dev:443",
            "tuf-repo-cdn.sigstore.dev:443",
            "fulcio.githubapp.com:443",
            "timestamp.githubapp.com:443",
            "pypi.org:443",
            "files.pythonhosted.org:443",
        },
    ),
    # Tag builds v0.37.15 to v0.37.19 (runs 45127023389 to 45228315022).
    "publish-pypi-on-tag.yml/pypi-build": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "releases.astral.sh:443",
            "pypi.org:443",
            "files.pythonhosted.org:443",
            "fulcio.sigstore.dev:443",
            "rekor.sigstore.dev:443",
        },
    ),
    # Main runs 45247358964 and 45228243674.
    "release-version-pr.yml/version-pr": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "pypi.org:443",
            "files.pythonhosted.org:443",
        },
    ),
    # Scheduled run 45224542146.
    "scorecards.yml/scorecards": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "codeload.github.com:443",
            "api.osv.dev:443",
            "api.deps.dev:443",
            "api.scorecard.dev:443",
            "fulcio.sigstore.dev:443",
            "rekor.sigstore.dev:443",
            "tuf-repo-cdn.sigstore.dev:443",
            "oss-fuzz-build-logs.storage.googleapis.com:443",
            "www.bestpractices.dev:443",
        },
    ),
    # Main run 45247358914. CodeQL bundle downloads redirect to
    # release-assets; without it a toolcache miss fails the job (2026-07-11).
    "codeql.yml/codeql": frozenset(
        {
            "github.com:443",
            "api.github.com:443",
            "uploads.github.com:443",
            "release-assets.githubusercontent.com:443",
        },
    ),
}


@dataclass(frozen=True)
class _Call:
    """One job that calls an lgtm-ci reusable workflow.

    Attributes:
        pin: Ref after the ``@`` in ``uses``.
        inputs: The job's ``with`` mapping.
    """

    pin: str
    inputs: dict[str, Any]


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
    )


def _names(hosts: set[str]) -> set[str]:
    """Strip the port from each ``host:port`` entry."""
    return {host.rsplit(":", 1)[0] if ":" in host else host for host in hosts}


def test_no_allow_list_names_a_known_dead_host() -> None:
    """One dead entry stops the hardening agent, whatever port it names."""
    for where, hosts in _all_allow_lists():
        assert_that(_names(hosts) & _KNOWN_DEAD_NAMES).described_as(where).is_empty()


def test_no_allow_list_admits_a_wildcard_host() -> None:
    """List exact hosts: a wildcard admits names anyone can register.

    ``*.blob.core.windows.net`` admitted every Azure storage account, so a
    compromised step could send the PyPI OIDC token to one (#432).
    """
    for where, hosts in _all_allow_lists():
        wildcards = {host for host in hosts if "*" in host}
        assert_that(wildcards).described_as(where).is_empty()


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


def test_no_hand_written_list_names_a_retired_host() -> None:
    """Drop retired names from every list a workflow writes itself."""
    for where, hosts in _all_allow_lists():
        assert_that(_names(hosts) & _RETIRED_NAMES).described_as(where).is_empty()


def test_no_preset_names_a_known_dead_host() -> None:
    """Hold the preset hosts read from lgtm-ci to the same dead-name rule."""
    for preset, hosts in _PRESET_HOSTS.items():
        assert_that(_names(set(hosts)) & _KNOWN_DEAD_NAMES).described_as(
            preset,
        ).is_empty()


def _workflows() -> dict[str, Any]:
    """Parse every checked-in workflow, keyed by file name."""
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(_WORKFLOWS.glob("*.y*ml"))
    }


def _reusable_calls(workflows: dict[str, Any]) -> dict[str, _Call]:
    """Return every job that calls an lgtm-ci reusable, keyed ``file/job``."""
    calls: dict[str, _Call] = {}
    for name, workflow in workflows.items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            uses = job.get("uses")
            match = _REUSABLE.match(uses.strip()) if isinstance(uses, str) else None
            if match is not None:
                calls[f"{name}/{job_id}"] = _Call(
                    pin=match.group("pin"),
                    inputs=job.get("with") or {},
                )
    return calls


def _listed(inputs: dict[str, Any], where: str) -> set[str]:
    """Read a call's ``allowed-endpoints`` input; none is an empty set."""
    value = inputs.get("allowed-endpoints")
    return set() if value is None else _hosts(value, where)


def _replaces_its_preset(inputs: dict[str, Any], where: str) -> bool:
    """Tell whether a non-empty list replaces the preset, as the reusables do."""
    listed = _listed(inputs, where)
    return bool(listed) and inputs.get("allowed-endpoints-mode") != "append"


def _effective_hosts(inputs: dict[str, Any], where: str) -> set[str]:
    """Return every host the reusable's hardened jobs run with.

    A replacement list is used alone. Otherwise the job gets the named
    preset's hosts plus the appended list. A call checked here must name its
    preset, and the preset must be in ``_PRESET_HOSTS``, so a preset whose
    hosts were not read fails instead of passing on an empty set.
    """
    listed = _listed(inputs, where)
    if _replaces_its_preset(inputs, where):
        return listed
    preset = inputs.get("egress-preset")
    if preset not in _PRESET_HOSTS:
        msg = f"{where}: name an egress-preset whose hosts are in _PRESET_HOSTS"
        raise LookupError(msg)
    return set(_PRESET_HOSTS[preset]) | listed


def _assert_no_unlisted_replacement(calls: dict[str, _Call]) -> None:
    """Require append mode, except for the reasoned replacement lists."""
    for where, call in calls.items():
        if _replaces_its_preset(call.inputs, where):
            assert_that(_REPLACEMENT_LISTS).described_as(
                f"{where} replaces its preset; append to it instead",
            ).contains_key(where)


def _assert_required_hosts_reachable(calls: dict[str, _Call]) -> None:
    """Require each listed call to reach its hosts through preset and list."""
    for where, required in _REQUIRED_HOSTS.items():
        assert_that(calls).described_as("required-host callers").contains_key(where)
        reachable = _effective_hosts(calls[where].inputs, where)
        assert_that(set(required) - reachable).described_as(where).is_empty()


def test_reusable_calls_do_not_replace_their_preset() -> None:
    """Append to the preset; a full replacement list must be a listed exception."""
    _assert_no_unlisted_replacement(_reusable_calls(_workflows()))


def test_every_replacement_exception_is_still_a_replacement() -> None:
    """Drop an exception once its call stops replacing the preset."""
    calls = _reusable_calls(_workflows())
    for where in _REPLACEMENT_LISTS:
        assert_that(calls).contains_key(where)
        assert_that(_replaces_its_preset(calls[where].inputs, where)).described_as(
            where,
        ).is_true()


def test_callers_still_reach_the_hosts_they_use() -> None:
    """Keep every host a migrated call depends on in its combined list."""
    _assert_required_hosts_reachable(_reusable_calls(_workflows()))


def test_preset_hosts_were_read_at_the_pinned_lgtm_ci_commit() -> None:
    """Fail a pin move until the preset hosts are read again."""
    pins = {call.pin for call in _reusable_calls(_workflows()).values()}
    assert_that(pins).is_equal_to({_LGTM_CI_COMMIT})


def _call(inputs: dict[str, Any]) -> dict[str, Any]:
    """Build a one-job workflow set that calls an lgtm-ci reusable."""
    job = {
        "uses": "lgtm-hq/lgtm-ci/.github/workflows/reusable-docker.yml@" + "a" * 40,
        "with": inputs,
    }
    return {"peer.yml": {"permissions": {}, "jobs": {"call": job}}}


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        ({"egress-preset": "github-minimal"}, _PRESET_HOSTS["github-minimal"]),
        (
            {
                "egress-preset": "github-minimal",
                "allowed-endpoints-mode": "append",
                "allowed-endpoints": "pypi.org:443\n",
            },
            _PRESET_HOSTS["github-minimal"] | {"pypi.org:443"},
        ),
        (
            {"egress-preset": "github-minimal", "allowed-endpoints": "pypi.org:443"},
            {"pypi.org:443"},
        ),
        (
            {
                "egress-preset": "docker",
                "allowed-endpoints-mode": "replace",
                "allowed-endpoints": "pypi.org:443",
            },
            {"pypi.org:443"},
        ),
    ],
    ids=["preset", "append", "replace-implicit", "replace-explicit"],
)
def test_effective_hosts_mirror_the_reusables(
    inputs: dict[str, Any],
    expected: frozenset[str],
) -> None:
    """Combine preset and list in append mode; use a replacement list alone."""
    assert_that(_effective_hosts(inputs, "x")).is_equal_to(set(expected))


@pytest.mark.parametrize(
    "inputs",
    [
        {"allowed-endpoints-mode": "append", "allowed-endpoints": "pypi.org:443"},
        {"egress-preset": "release-version-pr"},
    ],
    ids=["no-preset-named", "hosts-not-read"],
)
def test_effective_hosts_refuse_a_preset_they_cannot_read(
    inputs: dict[str, Any],
) -> None:
    """Fail rather than check a call against hosts nobody read."""
    assert_that(_effective_hosts).raises(LookupError).when_called_with(inputs, "x")


@pytest.mark.parametrize(
    "inputs",
    [
        {"allowed-endpoints": "github.com:443"},
        {"allowed-endpoints-mode": "replace", "allowed-endpoints": "github.com:443"},
        {"egress-preset": "docker", "allowed-endpoints": "github.com:443"},
    ],
    ids=["implicit", "explicit", "with-preset"],
)
def test_an_unlisted_replacement_list_fails(inputs: dict[str, Any]) -> None:
    """Reject a full list that replaces the preset outside the exceptions."""
    with pytest.raises(AssertionError):
        _assert_no_unlisted_replacement(_reusable_calls(_call(inputs)))


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"egress-preset": "docker"},
        {"allowed-endpoints-mode": "append", "allowed-endpoints": "pypi.org:443"},
        {"allowed-endpoints": " \n"},
    ],
    ids=["default", "preset", "append", "blank-list"],
)
def test_calls_that_keep_their_preset_pass(inputs: dict[str, Any]) -> None:
    """Pass the default, a named preset, append mode and a blank list."""
    _assert_no_unlisted_replacement(_reusable_calls(_call(inputs)))


def _without(where: str, host: str) -> dict[str, _Call]:
    """Return the real calls with ``host`` taken out of one call's own list."""
    calls = _reusable_calls(_workflows())
    inputs = dict(calls[where].inputs)
    kept = _listed(inputs, where) - {host}
    assert_that(kept).described_as(f"{where} lists {host}").is_not_equal_to(
        _listed(inputs, where),
    )
    inputs["allowed-endpoints"] = " ".join(sorted(kept))
    calls[where] = _Call(pin=calls[where].pin, inputs=inputs)
    return calls


@pytest.mark.parametrize(
    ("where", "host"),
    [
        ("docker-build-publish.yml/docker", "pypi.org:443"),
        ("docker-build-publish.yml/docker", "files.pythonhosted.org:443"),
        ("release-version-pr.yml/version-pr", "pypi.org:443"),
        ("scorecards.yml/scorecards", "www.bestpractices.dev:443"),
        ("scorecards.yml/scorecards", "oss-fuzz-build-logs.storage.googleapis.com:443"),
    ],
    ids=["docker-pypi", "docker-files", "version-pr", "scorecards", "scorecards-fuzz"],
)
def test_dropping_a_used_appended_host_fails(where: str, host: str) -> None:
    """A host the job uses cannot leave the appended list unnoticed."""
    with pytest.raises(AssertionError):
        _assert_required_hosts_reachable(_without(where, host))


def test_dropping_an_unused_fallback_host_passes() -> None:
    """Narrowing stays possible: astral.sh was not called by any tag build."""
    _assert_required_hosts_reachable(
        _without("publish-pypi-on-tag.yml/pypi-build", "astral.sh:443"),
    )


@pytest.mark.parametrize(
    ("preset", "host"),
    [
        ("docker", "docker-images-prod.s3.dualstack.us-east-1.amazonaws.com:443"),
        ("python-dist", "rekor.sigstore.dev:443"),
        ("github-minimal", "api.github.com:443"),
        ("github-tooling", "release-assets.githubusercontent.com:443"),
        ("scorecard", "api.osv.dev:443"),
    ],
    ids=["docker", "python-dist", "github-minimal", "github-tooling", "scorecard"],
)
def test_a_preset_losing_a_used_host_fails(
    preset: str,
    host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a preset change that drops a host a caller relies on."""
    monkeypatch.setitem(_PRESET_HOSTS, preset, _PRESET_HOSTS[preset] - {host})
    with pytest.raises(AssertionError):
        _assert_required_hosts_reachable(_reusable_calls(_workflows()))


def test_switching_back_to_a_short_replacement_list_fails() -> None:
    """Replace mode with only the extra hosts would lose the preset's hosts."""
    calls = _reusable_calls(_workflows())
    where = "docker-build-publish.yml/docker"
    inputs = dict(calls[where].inputs)
    inputs["allowed-endpoints-mode"] = "replace"
    calls[where] = _Call(pin=calls[where].pin, inputs=inputs)
    with pytest.raises(AssertionError):
        _assert_required_hosts_reachable(calls)
    with pytest.raises(AssertionError):
        _assert_no_unlisted_replacement(calls)


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
