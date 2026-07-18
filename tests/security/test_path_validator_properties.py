"""Property-based tests for path validation invariants.

These tests use Hypothesis to verify that
:class:`winnow.security.path_validator.PathValidator` upholds its core safety contract
across arbitrary inputs: paths composed of safe components that stay within the allowed
root are accepted and their resolved form remains within that root; paths that traverse
above the root via ``..`` segments are unconditionally rejected with
:class:`~winnow.exceptions.SecurityError`; and the configured
:class:`~winnow.security.enums.SymlinkPolicy` is enforced as documented.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from assertpy import assert_that
from hypothesis import given, settings
from hypothesis import strategies as st

from winnow.exceptions import SecurityError
from winnow.security.enums import SymlinkPolicy
from winnow.security.path_validator import PathValidator

_MAX_EXAMPLES = 50

_safe_component = st.from_regex(r"[a-zA-Z0-9_-]{1,10}", fullmatch=True)


@given(
    components=st.lists(
        _safe_component,
        min_size=1,
        max_size=5,
    ),
)
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_prop_paths_inside_root_stay_within_root(components: list[str]) -> None:
    """Resolved paths composed of safe components never escape the allowed root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = (Path(tmpdir) / "root").resolve()
        root.mkdir()
        target = root.joinpath(*components)
        validator = PathValidator(allowed_roots=[root])
        result = validator.validate_path(target)
        assert_that(result.is_relative_to(root)).is_true()


@given(
    components=st.lists(
        _safe_component,
        min_size=1,
        max_size=3,
    ),
)
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_prop_dotdot_escape_raises_security_error(components: list[str]) -> None:
    """Paths with enough '..' segments to escape the root are rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = (Path(tmpdir) / "root").resolve()
        root.mkdir()
        escaping = root.joinpath(
            *components,
            *[".."] * (len(components) + 1),
            "secret.txt",
        )
        validator = PathValidator(allowed_roots=[root])
        with pytest.raises(SecurityError, match="escapes the allowed roots"):
            validator.validate_path(escaping)


@given(filename=st.from_regex(r"[a-zA-Z0-9_-]{1,10}\.txt", fullmatch=True))
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_prop_symlink_policy_reject_raises_for_any_symlink(filename: str) -> None:
    """Under SymlinkPolicy.REJECT, any symlink component raises SecurityError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = (Path(tmpdir) / "root").resolve()
        root.mkdir()
        real_file = root / filename
        real_file.touch()
        link_file = root / ("link_" + filename)
        link_file.symlink_to(real_file)
        validator = PathValidator(
            allowed_roots=[root],
            symlink_policy=SymlinkPolicy.REJECT,
        )
        with pytest.raises(SecurityError, match="symlink traversal is not permitted"):
            validator.validate_path(link_file)


@given(filename=st.from_regex(r"[a-zA-Z0-9_-]{1,10}\.txt", fullmatch=True))
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_prop_symlink_policy_follow_accepts_in_root_symlinks(filename: str) -> None:
    """Under SymlinkPolicy.FOLLOW, symlinks pointing inside the root are accepted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = (Path(tmpdir) / "root").resolve()
        root.mkdir()
        real_file = root / filename
        real_file.touch()
        link_file = root / ("link_" + filename)
        link_file.symlink_to(real_file)
        validator = PathValidator(
            allowed_roots=[root],
            symlink_policy=SymlinkPolicy.FOLLOW,
        )
        result = validator.validate_path(link_file)
        assert_that(result.is_relative_to(root)).is_true()
