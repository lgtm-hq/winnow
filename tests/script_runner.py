"""Run repository scripts from tests.

The one place test modules launch a process for a script or build tool, so the
security review of that launch (fixed argv, no shell, explicit environment)
lives here once instead of in each test module.
"""

from __future__ import annotations

import subprocess  # nosec B404 - only run_script below; fixed argv, never a shell
from collections.abc import Mapping, Sequence
from pathlib import Path


def run_script(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv without a shell and capture its output as text.

    Args:
        argv: Executable path and arguments, chosen by the calling test.
        env: Complete environment for the process.
        cwd: Working directory for the process.
        timeout: Seconds before the process is killed and the call raises.
        check: Raise ``CalledProcessError`` when the process exits nonzero.

    Returns:
        The completed process with ``stdout`` and ``stderr`` as text.
    """
    return subprocess.run(  # nosec B603 - test-chosen fixed argv, shell=False
        list(argv),
        cwd=cwd,
        env=dict(env),
        shell=False,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
