"""Winnow exception hierarchy and error handling standards.

All winnow code should:

- Raise domain-specific :class:`WinnowError` subclasses with structured context.
- Catch specific exceptions only; never use bare ``except:``.
- Use context managers (``open``, ``TemporaryDirectory``, etc.) for resources.
- Chain underlying errors with ``raise ... from ...`` when wrapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True, slots=True, eq=True)
class ErrorContext:
    """Structured context attached to a :class:`WinnowError`."""

    operation: str | None = None
    file_path: Path | str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize details into an immutable mapping."""
        object.__setattr__(
            self,
            "details",
            MappingProxyType(dict(self.details)),
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-friendly dict representation of this context.

        Returns:
            Context fields that were explicitly set.
        """
        result: dict[str, object] = {}
        if self.operation is not None:
            result["operation"] = self.operation
        if self.file_path is not None:
            result["file_path"] = str(self.file_path)
        if self.details:
            result["details"] = dict(self.details)
        return result


class WinnowError(Exception):
    """Base exception for recoverable and fatal winnow failures.

    Args:
        message: Human-readable error description.
        operation: Name of the operation that failed, if applicable.
        file_path: Related file path, if applicable.
        details: Additional structured metadata about the failure.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        file_path: Path | str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context = ErrorContext(
            operation=operation,
            file_path=file_path,
            details=dict(details) if details is not None else {},
        )

    def __str__(self) -> str:
        """Format the error message with any structured context."""
        parts = [self.message]
        if self.context.operation is not None:
            parts.append(f"operation={self.context.operation}")
        if self.context.file_path is not None:
            parts.append(f"file_path={self.context.file_path}")
        if self.context.details:
            parts.append(f"details={dict(self.context.details)}")
        return " | ".join(parts)

    def as_dict(self) -> dict[str, object]:
        """Return a structured representation suitable for logging.

        Returns:
            Error type, message, and any populated context fields.
        """
        return {
            "type": type(self).__name__,
            "message": self.message,
            "context": self.context.as_dict(),
        }


class ConfigError(WinnowError):
    """Raised when configuration files or values are invalid."""


class MediaError(WinnowError):
    """Raised when media discovery or processing fails."""


class HashError(WinnowError):
    """Raised when content hashing fails."""


class CacheError(WinnowError):
    """Raised when cache read or write operations fail."""


class PipelineError(WinnowError):
    """Raised when a pipeline step fails."""


class SecurityError(WinnowError):
    """Raised for path validation and symlink policy violations."""


class DuplicateError(WinnowError):
    """Raised when duplicate detection or resolution fails."""


__all__ = [
    "CacheError",
    "ConfigError",
    "DuplicateError",
    "ErrorContext",
    "HashError",
    "MediaError",
    "PipelineError",
    "SecurityError",
    "WinnowError",
]
