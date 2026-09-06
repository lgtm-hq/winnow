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

from winnow.exceptions import SecurityError
from winnow.models.enums import SymlinkPolicy


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
            ``FOLLOW`` resolves them and validates the target; ``SKIP`` and
            ``ERROR`` both refuse to traverse any untrusted symlink by
            raising :class:`~winnow.exceptions.SecurityError`. Whether the
            caller then skips the path silently or records an error is the
            caller's concern.
        base_dir: Directory used to resolve relative candidate paths. Defaults
            to the current working directory when omitted.

    Raises:
        SecurityError: If no allowed roots are provided.
    """

    def __init__(
        self,
        allowed_roots: Iterable[Path | str],
        *,
        symlink_policy: SymlinkPolicy = SymlinkPolicy.SKIP,
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

        Validation resolves and inspects the path as it exists at call time.
        It is not a guarantee about the path's state during a subsequent
        filesystem operation: an attacker able to modify the tree could
        replace a validated component with a symlink between this call and a
        later ``open``. Callers that act on the returned path must perform the
        operation atomically relative to a trusted directory (for example with
        ``O_NOFOLLOW`` / ``openat``) rather than treating this result as a
        standalone safety guarantee.

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

        ``~`` and ``~user`` are not expanded: they are treated as literal
        relative components anchored under the base directory, which fails
        closed rather than silently escaping to a home directory.

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

        ``FOLLOW`` permits traversal; ``SKIP`` and ``ERROR`` both refuse it.

        Args:
            absolute: Absolute, unresolved candidate path.
            original: Original path supplied by the caller, for diagnostics.
            operation: Operation name recorded on any raised error.

        Raises:
            SecurityError: If an untrusted symlink is found and the policy
                is not ``FOLLOW``.
        """
        if self._symlink_policy is SymlinkPolicy.FOLLOW:
            return

        symlinks = self._untrusted_symlinks(absolute)
        if not symlinks:
            return

        raise SecurityError(
            "symlink traversal is not permitted",
            operation=operation,
            file_path=original,
            details={"symlinks": [str(symlink) for symlink in symlinks]},
        )

    def _untrusted_symlinks(self, absolute: Path) -> list[Path]:
        """Return every untrusted symlink component of ``absolute``.

        The candidate is scanned component by component from the filesystem
        anchor downward *without* collapsing ``..`` segments, so a symlink is
        detected even when a later ``..`` would resolve the path back inside a
        root (for example ``<root>/link/../file``). Only the trusted root
        prefix itself -- the first prefix whose resolved form is exactly a
        configured root, including a symlinked root alias -- is exempt, so a
        legitimate alias for the root itself is never reported. Every other
        symlink component is reported, including symlinks that appear *before*
        that boundary in the absolute path (for example an external symlink
        ``/tmp/link`` in ``/tmp/link/../<root>/file`` whose trailing ``..``
        navigates back into the root); those are still untrusted traversals.
        Aliases that resolve *beneath* a root (for example an external link
        targeting a root subdirectory) do not establish a boundary and are
        therefore reported.

        When the fully resolved candidate lies outside every allowed root, no
        symlink is reported; the containment check in :meth:`validate_path`
        then surfaces the real "escapes the allowed roots" violation instead
        of a misleading symlink error.

        Args:
            absolute: Absolute, unresolved candidate path.

        Returns:
            The untrusted symlink components in order from the anchor
            downward, or an empty list when there are none or the candidate
            resolves outside all roots.
        """
        if self._containing_root(absolute.resolve()) is None:
            return []

        prefixes = self._ancestor_prefixes(absolute)
        boundary_index = self._trusted_root_index(prefixes)
        return [
            prefix
            for index, prefix in enumerate(prefixes)
            if index != boundary_index and prefix.is_symlink()
        ]

    @staticmethod
    def _ancestor_prefixes(absolute: Path) -> list[Path]:
        """Return cumulative path prefixes from the anchor down to ``absolute``.

        Args:
            absolute: Absolute candidate path.

        Returns:
            Prefixes ordered from the filesystem anchor to the full path, with
            ``..`` segments preserved so symlink components are not hidden.
        """
        parts = absolute.parts
        current = Path(parts[0])
        prefixes = [current]
        for part in parts[1:]:
            current = current / part
            prefixes.append(current)
        return prefixes

    def _trusted_root_index(self, prefixes: list[Path]) -> int | None:
        """Return the index of the trusted root prefix, if one exists.

        The trusted prefix is the sole component exempt from symlink
        reporting: it marks a legitimate alias for the root itself. It does
        *not* extend trust to earlier components, so a symlink that precedes
        this boundary (reached via ``..``) is still reported by
        :meth:`_untrusted_symlinks`.

        Args:
            prefixes: Cumulative prefixes ordered from the anchor downward.

        Returns:
            The index of the first prefix whose resolved form is exactly a
            configured allowed root, or None when no prefix maps to a root.
        """
        roots = set(self._allowed_roots)
        for index, prefix in enumerate(prefixes):
            if prefix.resolve() in roots:
                return index
        return None
