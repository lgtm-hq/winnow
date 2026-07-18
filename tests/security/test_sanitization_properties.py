"""Property-based tests for filename sanitization invariants.

These tests use Hypothesis to verify that
:func:`winnow.security.filenames.sanitize_filename` upholds its core safety contract
across arbitrary inputs: no path separators or NUL bytes survive sanitization, the
operation is idempotent, and the result is never a filesystem meta-entry (``'.'`` or
``'..'``). A bounded extension-preservation property exercises the common happy-path:
a safe ``stem.ext`` round-trips its extension unchanged.
"""

from __future__ import annotations

from assertpy import assert_that
from hypothesis import given, settings
from hypothesis import strategies as st

from winnow.exceptions import SecurityError
from winnow.security.filenames import sanitize_filename

_MAX_EXAMPLES = 50


@given(name=st.text())
@settings(max_examples=_MAX_EXAMPLES)
def test_prop_sanitized_output_has_no_path_separators_or_nul(name: str) -> None:
    """Sanitized output never contains path separators or NUL bytes for any input."""
    try:
        result = sanitize_filename(name)
    except SecurityError:
        return
    assert_that(result).does_not_contain("/")
    assert_that(result).does_not_contain("\\")
    assert_that(result).does_not_contain("\x00")


@given(name=st.text())
@settings(max_examples=_MAX_EXAMPLES)
def test_prop_sanitization_is_idempotent(name: str) -> None:
    """sanitize(sanitize(x)) equals sanitize(x) when sanitization succeeds."""
    try:
        first = sanitize_filename(name)
    except SecurityError:
        return
    second = sanitize_filename(first)
    assert_that(second).is_equal_to(first)


@given(name=st.text())
@settings(max_examples=_MAX_EXAMPLES)
def test_prop_sanitized_output_is_never_dot_or_dotdot(name: str) -> None:
    """Sanitized output is never '.' or '..' for inputs that sanitize successfully."""
    try:
        result = sanitize_filename(name)
    except SecurityError:
        return
    assert_that(result).is_not_equal_to(".")
    assert_that(result).is_not_equal_to("..")


@given(
    stem=st.from_regex(r"[a-zA-Z0-9_-]{1,20}", fullmatch=True),
    ext=st.from_regex(r"\.[a-zA-Z0-9]{1,5}", fullmatch=True),
)
@settings(max_examples=_MAX_EXAMPLES)
def test_prop_valid_extension_preserved(
    stem: str,
    ext: str,
) -> None:
    """A safe stem.ext input retains its extension after sanitization."""
    result = sanitize_filename(stem + ext)
    assert_that(result).ends_with(ext)
