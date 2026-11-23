"""Tests for release distribution smoke script."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import cast

import pytest
from assertpy import assert_that

from tests.script_runner import run_script
from winnow import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SMOKE_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "release" / "smoke-test-distribution.sh"
_CLI_COMMAND = "winnow"
# Generous ceiling for a wheel build or install; only a hung step reaches it.
_TIMEOUT_SECONDS = 900


def _require_executable(name: str) -> str:
    """Return an absolute executable path or fail the test."""
    executable = shutil.which(name)
    assert_that(executable).described_as(f"{name} must be on PATH").is_not_none()
    return cast(str, executable)


@pytest.mark.integration
def test_smoke_test_distribution_installs_wheel_and_reports_version(
    tmp_path: Path,
) -> None:
    """Built wheel passes the release smoke test without system-site install."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    uv_bin = _require_executable("uv")
    bash_bin = _require_executable("bash")

    run_script(
        [uv_bin, "build", "--out-dir", str(dist_dir)],
        env=os.environ,
        cwd=_REPO_ROOT,
        timeout=_TIMEOUT_SECONDS,
        check=True,
    )

    result = run_script(
        [bash_bin, str(_SMOKE_SCRIPT)],
        env={
            **os.environ,
            "DIST_PATH": str(dist_dir),
            "EXPECTED_VERSION": __version__,
            "CLI_COMMAND": _CLI_COMMAND,
        },
        cwd=_REPO_ROOT,
        timeout=_TIMEOUT_SECONDS,
    )

    assert_that(result.returncode).described_as(result.stderr).is_equal_to(0)
    assert_that(result.stdout).contains("Smoke test passed")
