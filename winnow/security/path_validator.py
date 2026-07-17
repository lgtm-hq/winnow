"""Path validation and symlink protection for the winnow security domain.

:class:`PathValidator` resolves and normalizes candidate paths, enforces that
they stay within a set of allowed roots (for example, configured source and
destination directories), and applies a configurable symlink policy. Any
violation raises :class:`~winnow.exceptions.SecurityError` so that callers can
handle path safety failures uniformly.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from loguru import logger

from winnow.exceptions import SecurityError
from winnow.security.enums import SymlinkPolicy


class PathValidator:
    """Validate filesystem paths against allowed roots and a symlink policy.

    The validator resolves each candidate path (normalizing ``..`` segments
    and, depending on policy, symlinks), then confirms the result is contained
    within one of the configured roots. This prevents directory traversal
    escapes and controls whether symbolic links may be traversed.

    Args:
        allowed_roots: Directories that operations are confined to. Each root
            is resolved during construction. At least one root is required.
        symlink_policy: How to treat symlinks encountered along a path.
        base_dir: Directory used to resolve relative candidate paths. Defaults
            to the current working directory when omitted.

    Raises:
        SecurityError: If no allowed roots are provided.
    """

    def __init__(
        self,
        allowed_roots: Iterable[Path | str],
        *,
        symlink_policy: SymlinkPolicy = SymlinkPolicy.REJECT,
        base_dir: Path | str | None = None,
    ) -> None:
        roots = tuple(Path(root).resolve() for root in allowed_roots)
        if not roots:
            raise SecurityError(
                "at least one allowed root is required",
                operation="configure_path_validator",
            )
        self._allowed_roots: tuple[Path, ...] = roots
        self._symlink_policy = symlink_policy
        self._base_dir = (
            Path(base_dir).resolve() if base_dir is not None else Path.cwd()
        )

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        """Return the resolved roots operations are confined to.

        Returns:
            The tuple of resolved allowed root directories.
        """
        return self._allowed_roots

    @property
    def symlink_policy(self) -> SymlinkPolicy:
        """Return the configured symlink policy.

        Returns:
            The active :class:`SymlinkPolicy`.
        """
        return self._symlink_policy

    def is_within_roots(self, path: Path | str) -> bool:
        """Return whether ``path`` resolves inside an allowed root.

        This performs no symlink policy enforcement; it only reports whether
        the resolved location is contained by a configured root.

        Args:
            path: Candidate path to test.

        Returns:
            True if the resolved path is inside an allowed root.
        """
        resolved = self._to_absolute(path).resolve()
        return self._containing_root(resolved) is not None

    def validate_path(
        self,
        path: Path | str,
        *,
        operation: str = "validate_path",
    ) -> Path:
        """Validate a candidate path and return its resolved location.

        Args:
            path: Candidate path to validate. May be absolute or relative to
                the validator's base directory, and need not yet exist.
            operation: Operation name recorded on any raised error for
                diagnostic context.

        Returns:
            The fully resolved, normalized path.

        Raises:
            SecurityError: If the path escapes the allowed roots or violates
                the configured symlink policy.
        """
        absolute = self._to_absolute(path)
        self._enforce_symlink_policy(
            absolute=absolute,
            original=path,
            operation=operation,
        )

        resolved = absolute.resolve()
        if self._containing_root(resolved) is None:
            raise SecurityError(
                "path escapes the allowed roots",
                operation=operation,
                file_path=path,
                details={
                    "resolved": str(resolved),
                    "allowed_roots": [str(root) for root in self._allowed_roots],
                },
            )
        return resolved

    def _to_absolute(self, path: Path | str) -> Path:
        """Return an absolute path without resolving symlinks.

        Args:
            path: Candidate path, absolute or relative.

        Returns:
            The candidate anchored to the base directory when relative.
        """
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return self._base_dir / candidate

    def _containing_root(self, resolved: Path) -> Path | None:
        """Return the allowed root that contains ``resolved``, if any.

        Args:
            resolved: A fully resolved candidate path.

        Returns:
            The first allowed root containing the path, or None.
        """
        return next(
            (root for root in self._allowed_roots if resolved.is_relative_to(root)),
            None,
        )

    def _enforce_symlink_policy(
        self,
        *,
        absolute: Path,
        original: Path | str,
        operation: str,
    ) -> None:
        """Apply the configured symlink policy to a candidate path.

        Args:
            absolute: Absolute, unresolved candidate path.
            original: Original path supplied by the caller, for diagnostics.
            operation: Operation name recorded on any raised error.

        Raises:
            SecurityError: If a symlink is found and the policy rejects it.
        """
        if self._symlink_policy is SymlinkPolicy.FOLLOW:
            return

        symlink = self._first_symlink(absolute)
        if symlink is None:
            return

        if self._symlink_policy is SymlinkPolicy.REJECT:
            raise SecurityError(
                "symlink traversal is not permitted",
                operation=operation,
                file_path=original,
                details={"symlink": str(symlink)},
            )

        logger.warning(
            "Traversing symlink during {operation}: {symlink}",
            operation=operation,
            symlink=str(symlink),
        )

    def _first_symlink(self, absolute: Path) -> Path | None:
        """Return the first untrusted symlink below the containing root.

        The walk starts at the candidate and moves upward, stopping at the
        ancestor that maps to an allowed root so that symlinks in trusted
        parent directories (including a symlinked root alias) are not
        misreported. When the candidate lies outside every allowed root, no
        symlink is reported; the containment check in
        :meth:`validate_path` then surfaces the real "escapes the allowed
        roots" violation instead of a misleading symlink error.

        Args:
            absolute: Absolute, unresolved candidate path.

        Returns:
            The first symlink found below the containing root, or None when
            there is none or the candidate is outside all roots.
        """
        boundary = self._root_boundary(absolute)
        if boundary is None:
            return None
        for ancestor in (absolute, *absolute.parents):
            if ancestor == boundary:
                break
            if ancestor.is_symlink():
                return ancestor
        return None

    def _root_boundary(self, absolute: Path) -> Path | None:
        """Return the ancestor of ``absolute`` that maps to an allowed root.

        Ancestors are examined from the candidate upward and resolved so that
        a root reached through a symlinked alias (for example, ``/linked``
        pointing at a resolved root ``/real``) is still recognized. This marks
        the boundary between the trusted root and the untrusted components the
        caller supplied beneath it.

        Args:
            absolute: Absolute, unresolved candidate path.

        Returns:
            The ancestor whose resolved form equals an allowed root, or None
            when the candidate resolves outside every allowed root.
        """
        roots = set(self._allowed_roots)
        for ancestor in (absolute, *absolute.parents):
            if ancestor.resolve() in roots:
                return ancestor
        return None
