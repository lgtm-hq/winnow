"""Tests for the GHCR cleanup's package existence probe.

``scripts/ci/maintenance/check-ghcr-package.sh`` gates
``ghcr-cleanup.yml``'s prune job. Only a definite answer from the owner's
package endpoint may write ``exists``: HTTP 200 is true and HTTP 404 is false.
Authentication, rate-limit, server, transport and malformed results must fail
the job with nothing written, so they can never pass as package absence (#360).

Each test runs the real script with a mock ``gh`` first on ``PATH`` and an
inert token; no request leaves the machine and nothing is deleted.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from assertpy import assert_that

from tests.script_runner import run_script

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "maintenance" / "check-ghcr-package.sh"
# Inert, random per session: never a real credential, and unique enough that
# finding it in the script's output proves a leak.
_TOKEN = f"inert-{secrets.token_hex(16)}"
_OWNER = "acme"
_PACKAGE = "winnow"
_LOOKUP_CALL = f"api users/{_OWNER} --jq .type"
_ORG_CALL = f"api --include orgs/{_OWNER}/packages/container/{_PACKAGE}"
_USER_CALL = f"api --include users/{_OWNER}/packages/container/{_PACKAGE}"

# Mock gh. The owner-type lookup (the call with --jq) answers MOCK_LOOKUP: a
# type name, an HTTP status, or "transport". Every other call answers
# MOCK_PACKAGE the way gh api --include does: the status line first on stdout,
# then headers and body; on HTTP errors gh also writes to stderr and exits 1.
# "transport" fails before any response, echoing the token so the test can
# prove the script redacts it; "malformed" prints no status line; "200fail"
# prints HTTP 200 but exits 1.
_MOCK_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$MOCK_GH_LOG"
reason() {
	case "$1" in
	200) echo OK ;;
	401) echo Unauthorized ;;
	403) echo Forbidden ;;
	404) echo "Not Found" ;;
	429) echo "Too Many Requests" ;;
	500) echo "Internal Server Error" ;;
	*) echo "Bad Gateway" ;;
	esac
}
transport() {
	echo "error connecting to api.github.com (token $GH_TOKEN)" >&2
	exit 1
}
padded() {
	printf '%285s(token %s)\n' "" "$GH_TOKEN" >&2
	exit 1
}
if [[ " $* " == *" --jq "* ]]; then
	case "$MOCK_LOOKUP" in
	transport) transport ;;
	[0-9][0-9][0-9])
		echo "gh: $(reason "$MOCK_LOOKUP") (HTTP $MOCK_LOOKUP)" >&2
		exit 1
		;;
	*) printf '%s\n' "$MOCK_LOOKUP" ;;
	esac
	exit 0
fi
case "$MOCK_PACKAGE" in
transport) transport ;;
padded) padded ;;
malformed)
	echo "<html>upstream proxy error</html>"
	exit 0
	;;
200fail)
	printf 'HTTP/2.0 200 OK\nContent-Type: application/json\r\n\r\n{}\n'
	echo "gh: unexpected end of JSON input" >&2
	exit 1
	;;
esac
status="$MOCK_PACKAGE"
printf 'HTTP/2.0 %s %s\n' "$status" "$(reason "$status")"
printf 'Content-Type: application/json\r\n\r\n{"message":"%s"}\n' "$(reason "$status")"
if [[ "$status" != 200 ]]; then
	echo "gh: $(reason "$status") (HTTP $status)" >&2
	exit 1
fi
"""


@dataclass(frozen=True)
class ProbeRun:
    """Outcome of one run of the probe script.

    Attributes:
        returncode: Exit status of the script.
        stdout: Captured standard output.
        stderr: Captured standard error.
        output: Contents of ``GITHUB_OUTPUT`` after the run.
        calls: Arguments of each mock ``gh`` call, in order.
    """

    returncode: int
    stdout: str
    stderr: str
    output: str
    calls: list[str]


def _bash() -> str:
    """Return the absolute path of bash or fail the test.

    Returns:
        Path of the bash executable found on ``PATH``.
    """
    executable = shutil.which("bash")
    assert_that(executable).described_as("bash must be on PATH").is_not_none()
    return cast(str, executable)


def _run_probe(
    tmp_path: Path,
    *,
    lookup: str = "Organization",
    package: str = "200",
    overrides: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
    args: tuple[str, ...] = (),
) -> ProbeRun:
    """Run the probe script against the mock ``gh``.

    Args:
        tmp_path: Per-test directory for the mock, its log and the output file.
        lookup: Mock answer to the owner-type lookup.
        package: Mock answer to the package endpoint call.
        overrides: Environment values to add or replace.
        unset: Environment variables to remove before the run.
        args: Command-line arguments for the script.

    Returns:
        The exit status, captured streams, ``GITHUB_OUTPUT`` and gh calls.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock = bin_dir / "gh"
    mock.write_text(_MOCK_GH, encoding="utf-8")
    mock.chmod(0o755)
    log = tmp_path / "gh-calls.log"
    log.touch()
    output = tmp_path / "github-output"
    output.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GH_TOKEN": _TOKEN,
        "OWNER": _OWNER,
        "PACKAGE_NAME": _PACKAGE,
        "GITHUB_OUTPUT": str(output),
        "MOCK_GH_LOG": str(log),
        "MOCK_LOOKUP": lookup,
        "MOCK_PACKAGE": package,
        **(overrides or {}),
    }
    for name in unset:
        env.pop(name)
    result = run_script(
        [_bash(), str(_SCRIPT), *args],
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    return ProbeRun(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        output=output.read_text(encoding="utf-8"),
        calls=log.read_text(encoding="utf-8").splitlines(),
    )


def _assert_failed_without_output(run: ProbeRun) -> None:
    """Assert the probe failed, wrote nothing and leaked no token.

    Args:
        run: Outcome of the probe run.
    """
    assert_that(run.returncode).described_as(run.stderr).is_equal_to(1)
    assert_that(run.output).is_empty()
    assert_that(run.stdout + run.stderr).does_not_contain(_TOKEN)


def test_forbidden_is_not_absence(tmp_path: Path) -> None:
    """HTTP 403 on every call fails the job instead of reporting absence."""
    run = _run_probe(tmp_path, lookup="403", package="403")

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains("HTTP 403")


def test_org_package_found_writes_true(tmp_path: Path) -> None:
    """An org owner is probed on the org endpoint; HTTP 200 writes true."""
    run = _run_probe(tmp_path, lookup="Organization", package="200")

    assert_that(run.returncode).described_as(run.stderr).is_equal_to(0)
    assert_that(run.output).is_equal_to("exists=true\n")
    assert_that(run.calls).is_equal_to([_LOOKUP_CALL, _ORG_CALL])


def test_user_package_found_writes_true(tmp_path: Path) -> None:
    """A user owner is probed on the user endpoint; HTTP 200 writes true."""
    run = _run_probe(tmp_path, lookup="User", package="200")

    assert_that(run.returncode).described_as(run.stderr).is_equal_to(0)
    assert_that(run.output).is_equal_to("exists=true\n")
    assert_that(run.calls).is_equal_to([_LOOKUP_CALL, _USER_CALL])


@pytest.mark.parametrize(
    ("owner_type", "expected_call"),
    [("org", _ORG_CALL), ("user", _USER_CALL)],
)
def test_owner_type_skips_lookup(
    tmp_path: Path,
    owner_type: str,
    expected_call: str,
) -> None:
    """OWNER_TYPE selects the endpoint without an owner lookup."""
    run = _run_probe(
        tmp_path,
        lookup="403",
        overrides={"OWNER_TYPE": owner_type},
    )

    assert_that(run.returncode).described_as(run.stderr).is_equal_to(0)
    assert_that(run.output).is_equal_to("exists=true\n")
    assert_that(run.calls).is_equal_to([expected_call])


def test_selected_endpoint_404_writes_false(tmp_path: Path) -> None:
    """HTTP 404 from the selected endpoint is the only clean skip."""
    run = _run_probe(tmp_path, lookup="Organization", package="404")

    assert_that(run.returncode).described_as(run.stderr).is_equal_to(0)
    assert_that(run.output).is_equal_to("exists=false\n")
    assert_that(run.calls).is_equal_to([_LOOKUP_CALL, _ORG_CALL])
    assert_that(run.stdout).contains("HTTP 404", "private package")


@pytest.mark.parametrize("status", ["401", "403", "429", "500", "502"])
def test_http_error_fails_without_fallback(tmp_path: Path, status: str) -> None:
    """An HTTP error fails, names the operation and tries no other endpoint."""
    run = _run_probe(tmp_path, lookup="Organization", package=status)

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains(
        f"GET orgs/{_OWNER}/packages/container/{_PACKAGE}",
        f"HTTP {status}",
    )
    assert_that(run.calls).is_equal_to([_LOOKUP_CALL, _ORG_CALL])


def test_transport_failure_fails_and_redacts_token(tmp_path: Path) -> None:
    """A transport error fails, and gh's message reaches the log redacted."""
    run = _run_probe(tmp_path, lookup="Organization", package="transport")

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains(
        "no HTTP status",
        "error connecting to api.github.com",
        "[redacted]",
    )


def test_token_across_the_clip_point_is_redacted(tmp_path: Path) -> None:
    """A token that straddles the 300-character clip leaves no part behind."""
    run = _run_probe(tmp_path, lookup="Organization", package="padded")

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains("[redacte").does_not_contain(_TOKEN[:8])


def test_malformed_output_fails(tmp_path: Path) -> None:
    """Output without an HTTP status line fails even when gh exits 0."""
    run = _run_probe(tmp_path, lookup="Organization", package="malformed")

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains("no HTTP status")


def test_ok_status_with_gh_failure_fails(tmp_path: Path) -> None:
    """HTTP 200 does not count when gh itself exits nonzero."""
    run = _run_probe(tmp_path, lookup="Organization", package="200fail")

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains("HTTP 200", "gh exited 1")


@pytest.mark.parametrize(
    ("lookup", "message"),
    [
        ("403", "HTTP 403"),
        ("500", "HTTP 500"),
        ("transport", "error connecting to api.github.com"),
        ("Bot", "unexpected type Bot"),
    ],
)
def test_owner_lookup_failure_fails(
    tmp_path: Path,
    lookup: str,
    message: str,
) -> None:
    """A failed owner-type lookup fails before any package call."""
    run = _run_probe(tmp_path, lookup=lookup, package="404")

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains(f"GET users/{_OWNER}", message)
    assert_that(run.calls).is_equal_to([_LOOKUP_CALL])


def test_invalid_owner_type_fails(tmp_path: Path) -> None:
    """An OWNER_TYPE other than org or user fails before any gh call."""
    run = _run_probe(tmp_path, overrides={"OWNER_TYPE": "enterprise"})

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains("OWNER_TYPE must be org or user")
    assert_that(run.calls).is_empty()


def test_owner_outside_login_syntax_fails(tmp_path: Path) -> None:
    """An OWNER that is not a GitHub login fails before any gh call."""
    run = _run_probe(tmp_path, overrides={"OWNER": "acme/../repos"})

    _assert_failed_without_output(run)
    assert_that(run.stderr).contains("OWNER must be a GitHub login")
    assert_that(run.calls).is_empty()


def test_owner_login_with_underscore_is_accepted(tmp_path: Path) -> None:
    """A managed-user login, which has an underscore, is a valid OWNER."""
    run = _run_probe(
        tmp_path,
        overrides={"OWNER": "mona_acme", "OWNER_TYPE": "user"},
    )

    assert_that(run.returncode).described_as(run.stderr).is_equal_to(0)
    assert_that(run.output).is_equal_to("exists=true\n")
    assert_that(run.calls).is_equal_to(
        ["api --include users/mona_acme/packages/container/winnow"],
    )


def test_slash_package_name_is_encoded(tmp_path: Path) -> None:
    """Slashes in the package name are URL-encoded in the endpoint."""
    run = _run_probe(tmp_path, overrides={"PACKAGE_NAME": "winnow/cli/tools"})

    assert_that(run.returncode).described_as(run.stderr).is_equal_to(0)
    assert_that(run.output).is_equal_to("exists=true\n")
    assert_that(run.calls).is_equal_to(
        [
            _LOOKUP_CALL,
            f"api --include orgs/{_OWNER}/packages/container/winnow%2Fcli%2Ftools",
        ],
    )


@pytest.mark.parametrize("name", ["OWNER", "PACKAGE_NAME", "GITHUB_OUTPUT"])
def test_missing_variable_fails(tmp_path: Path, name: str) -> None:
    """Each required variable is checked before any gh call."""
    run = _run_probe(tmp_path, unset=(name,))

    assert_that(run.returncode).is_not_equal_to(0)
    assert_that(run.stderr).contains(f"{name} is required")
    assert_that(run.output).is_empty()
    assert_that(run.calls).is_empty()


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage(tmp_path: Path, flag: str) -> None:
    """--help and -h print usage and make no gh call."""
    run = _run_probe(tmp_path, unset=("OWNER", "PACKAGE_NAME"), args=(flag,))

    assert_that(run.returncode).is_equal_to(0)
    assert_that(run.stdout).contains("Usage:", "OWNER_TYPE", "exists=false")
    assert_that(run.output).is_empty()
    assert_that(run.calls).is_empty()
