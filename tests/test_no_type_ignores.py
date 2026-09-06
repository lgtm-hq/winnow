"""Guard against mypy suppression comments in package and test code.

ADR 0001 requires strict mypy with zero suppression comments. The marker is
assembled at runtime so this file does not trip the guard (or a plain grep).
"""

from __future__ import annotations

from pathlib import Path

from assertpy import assert_that

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MARKER = "type: " + "ignore"


def test_no_type_ignore_comments() -> None:
    """No file under winnow/ or tests/ contains a mypy suppression comment."""
    offenders = [
        f"{path}:{lineno}"
        for directory in ("winnow", "tests")
        for path in sorted((_REPO_ROOT / directory).rglob("*.py"))
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if _MARKER in line
    ]

    assert_that(offenders).described_as(
        f"'{_MARKER}' comments found: {offenders}",
    ).is_empty()
