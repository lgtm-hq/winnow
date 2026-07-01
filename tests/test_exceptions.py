"""Tests for the winnow exception hierarchy."""

from __future__ import annotations

import ast
from collections.abc import MutableMapping
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest
from assertpy import assert_that

import winnow
from winnow.exceptions import (
    CacheError,
    ConfigError,
    DuplicateError,
    ErrorContext,
    HashError,
    MediaError,
    PipelineError,
    SecurityError,
    WinnowError,
)

EXCEPTION_TYPES: tuple[tuple[type[WinnowError], str], ...] = (
    (ConfigError, "ConfigError"),
    (MediaError, "MediaError"),
    (HashError, "HashError"),
    (CacheError, "CacheError"),
    (PipelineError, "PipelineError"),
    (SecurityError, "SecurityError"),
    (DuplicateError, "DuplicateError"),
)


def test_winnow_error_stores_structured_context(tmp_path: Path) -> None:
    """WinnowError preserves message and structured context fields."""
    media_path = tmp_path / "photo.jpg"
    error = WinnowError(
        "failed to read metadata",
        operation="extract_metadata",
        file_path=media_path,
        details={"codec": "hevc", "attempt": 1},
    )

    assert_that(error.message).is_equal_to("failed to read metadata")
    assert_that(error.context.operation).is_equal_to("extract_metadata")
    assert_that(error.context.file_path).is_equal_to(media_path)
    assert_that(error.context.details).is_equal_to(
        {"codec": "hevc", "attempt": 1},
    )


def test_winnow_error_str_includes_context_fields(tmp_path: Path) -> None:
    """String formatting includes populated context fields."""
    media_path = tmp_path / "clip.mov"
    error = WinnowError(
        "hash mismatch",
        operation="verify_hash",
        file_path=media_path,
        details={"algorithm": "blake3"},
    )

    rendered = str(error)

    assert_that(rendered).contains("hash mismatch")
    assert_that(rendered).contains("operation=verify_hash")
    assert_that(rendered).contains(f"file_path={media_path}")
    assert_that(rendered).contains("details={'algorithm': 'blake3'}")
    assert_that(rendered.index("operation=verify_hash")).is_less_than(
        rendered.index(f"file_path={media_path}"),
    )


def test_winnow_error_as_dict_includes_type_and_context(tmp_path: Path) -> None:
    """Structured dict output includes error type and context payload."""
    config_path = tmp_path / ".winnow-config.yaml"
    error = ConfigError(
        "missing required key",
        operation="load_config",
        file_path=config_path,
        details={"key": "hash.algorithm"},
    )

    payload = error.as_dict()

    assert_that(payload).is_equal_to(
        {
            "type": "ConfigError",
            "message": "missing required key",
            "context": {
                "operation": "load_config",
                "file_path": str(config_path),
                "details": {"key": "hash.algorithm"},
            },
        },
    )


def test_error_context_as_dict_omits_empty_fields() -> None:
    """ErrorContext omits unset fields from its dict representation."""
    context = ErrorContext(operation="scan")

    assert_that(context.as_dict()).is_equal_to({"operation": "scan"})


def test_error_context_details_are_immutable() -> None:
    """ErrorContext copies and freezes details so callers cannot mutate them."""
    mutable_details = {"codec": "hevc"}
    context = ErrorContext(details=mutable_details)

    mutable_details["injected"] = "value"

    assert_that(context.details).is_equal_to({"codec": "hevc"})
    assert_that(isinstance(context.details, MappingProxyType)).is_true()
    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], context.details)["injected"] = "value"


def test_winnow_error_as_dict_always_includes_context_key() -> None:
    """Structured dict output always includes a context object."""
    payload = WinnowError("simple failure").as_dict()

    assert_that(payload).is_equal_to(
        {
            "type": "WinnowError",
            "message": "simple failure",
            "context": {},
        },
    )


def test_winnow_error_supports_exception_chaining() -> None:
    """Underlying exceptions can be chained with raise-from."""
    original = OSError("device offline")

    with pytest.raises(MediaError) as exc_info:
        raise MediaError(
            "could not open media file",
            operation="open_file",
            details={"errno": original.errno},
        ) from original

    assert_that(exc_info.value.__cause__).is_equal_to(original)


@pytest.mark.parametrize(("exception_type", "expected_name"), EXCEPTION_TYPES)
def test_domain_exceptions_inherit_from_winnow_error(
    exception_type: type[WinnowError],
    expected_name: str,
) -> None:
    """Each domain exception is a WinnowError subclass with preserved context."""
    error = exception_type(
        f"{expected_name} failure",
        operation="test_operation",
        details={"reason": "unit test"},
    )

    assert_that(error).is_instance_of(WinnowError)
    assert_that(type(error).__name__).is_equal_to(expected_name)
    assert_that(error.context.operation).is_equal_to("test_operation")
    assert_that(error.context.details).is_equal_to({"reason": "unit test"})


def test_domain_exceptions_can_be_caught_by_base_type() -> None:
    """Callers can handle all winnow failures via WinnowError."""
    caught: WinnowError | None = None

    try:
        raise SecurityError(
            "path escapes sandbox",
            operation="validate_path",
            file_path="/etc/passwd",
        )
    except WinnowError as error:
        caught = error

    assert_that(caught).is_not_none()
    if caught is None:
        pytest.fail("expected WinnowError to be caught")
    assert_that(caught).is_instance_of(SecurityError)


def test_no_bare_except_in_winnow_source() -> None:
    """Winnow source must not use bare except clauses."""
    package_root = Path(winnow.__file__).resolve().parent
    repo_root = package_root.parent
    source_files = list(package_root.rglob("*.py"))

    assert_that(source_files).is_not_empty()

    offenders = [
        f"{source_path.relative_to(repo_root)}:{node.lineno}"
        for source_path in source_files
        for node in ast.walk(
            ast.parse(source_path.read_text(encoding="utf-8")),
        )
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]

    assert_that(offenders).is_empty()
