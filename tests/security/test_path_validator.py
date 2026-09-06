"""Tests for the path validator and symlink protection."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from assertpy import assert_that

import winnow.models.enums
import winnow.security
from winnow.exceptions import SecurityError
from winnow.models.enums import SymlinkPolicy
from winnow.security.path_validator import PathValidator


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Create and return an allowed root directory.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        A resolved directory used as the sole allowed root.
    """
    allowed = tmp_path / "root"
    allowed.mkdir()
    return allowed.resolve()


def test_validate_path_accepts_path_inside_root(root: Path) -> None:
    """A path within an allowed root resolves successfully."""
    validator = PathValidator(allowed_roots=[root])
    target = root / "album" / "photo.jpg"

    assert_that(validator.validate_path(target)).is_equal_to(target.resolve())


def test_validate_path_accepts_nonexistent_path_inside_root(root: Path) -> None:
    """A not-yet-created path inside a root is still valid."""
    validator = PathValidator(allowed_roots=[root])
    target = root / "does" / "not" / "exist.png"

    assert_that(validator.validate_path(target)).is_equal_to(target.resolve())


def test_validate_path_rejects_directory_traversal_escape(root: Path) -> None:
    """A ``..`` traversal that escapes the root is rejected."""
    validator = PathValidator(allowed_roots=[root])
    escaping = root / ".." / "secret.txt"

    with pytest.raises(SecurityError, match="escapes the allowed roots"):
        validator.validate_path(escaping)


def test_validate_path_rejects_path_outside_root(root: Path, tmp_path: Path) -> None:
    """A path outside every allowed root is rejected."""
    validator = PathValidator(allowed_roots=[root])
    outside = tmp_path / "other" / "file.jpg"

    with pytest.raises(SecurityError, match="escapes the allowed roots"):
        validator.validate_path(outside)


def test_validate_path_resolves_relative_against_base_dir(root: Path) -> None:
    """Relative candidates resolve against the configured base directory."""
    validator = PathValidator(allowed_roots=[root], base_dir=root)

    assert_that(validator.validate_path("album/photo.jpg")).is_equal_to(
        (root / "album" / "photo.jpg").resolve(),
    )


def test_validate_path_supports_multiple_roots(root: Path, tmp_path: Path) -> None:
    """A path inside any configured root is accepted."""
    second = tmp_path / "second"
    second.mkdir()
    validator = PathValidator(allowed_roots=[root, second])
    target = second / "clip.mov"

    assert_that(validator.validate_path(target)).is_equal_to(target.resolve())


@pytest.mark.parametrize(
    "name",
    [
        "résumé_photo.jpg",
        "x" * 200 + ".png",
        "weird name (copy) [1]!.jpeg",
    ],
    ids=["unicode", "long", "special_chars"],
)
def test_validate_path_accepts_unicode_long_and_special_names(
    root: Path,
    name: str,
) -> None:
    """Unicode, long, and special-character names inside a root are valid."""
    validator = PathValidator(allowed_roots=[root])
    target = root / name

    assert_that(validator.validate_path(target)).is_equal_to(target.resolve())


def test_validate_path_rejects_symlink_under_skip_policy(root: Path) -> None:
    """Under SKIP policy, a symlink inside the root is rejected."""
    real = root / "real.jpg"
    real.touch()
    link = root / "link.jpg"
    link.symlink_to(real)
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.SKIP,
    )

    with pytest.raises(SecurityError, match="symlink traversal is not permitted"):
        validator.validate_path(link)


def test_validate_path_rejects_symlinked_parent_under_skip_policy(
    root: Path,
) -> None:
    """A symlinked intermediate directory is rejected under SKIP policy."""
    real_dir = root / "real_dir"
    real_dir.mkdir()
    (real_dir / "photo.jpg").touch()
    link_dir = root / "link_dir"
    link_dir.symlink_to(real_dir)
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.SKIP,
    )

    with pytest.raises(SecurityError, match="symlink traversal is not permitted"):
        validator.validate_path(link_dir / "photo.jpg")


def test_validate_path_rejects_symlink_followed_by_dotdot_under_skip_policy(
    root: Path,
) -> None:
    """A symlink neutralized by a later ``..`` is still rejected under SKIP."""
    real_sub = root / "real_sub"
    real_sub.mkdir()
    link_dir = root / "link_dir"
    link_dir.symlink_to(real_sub)
    (root / "photo.jpg").touch()
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.SKIP,
    )

    with pytest.raises(SecurityError, match="symlink traversal is not permitted"):
        validator.validate_path(link_dir / ".." / "photo.jpg")


def test_validate_path_rejects_external_alias_to_root_subdirectory(
    root: Path,
    tmp_path: Path,
) -> None:
    """An external symlink targeting a root subdirectory is rejected."""
    subdir = root / "subdir"
    subdir.mkdir()
    (subdir / "photo.jpg").touch()
    external_link = tmp_path / "outside_link"
    external_link.symlink_to(subdir)
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.SKIP,
    )

    with pytest.raises(SecurityError, match="symlink traversal is not permitted"):
        validator.validate_path(external_link / "photo.jpg")


def test_validate_path_rejects_external_symlink_navigating_into_root_via_dotdot(
    root: Path,
    tmp_path: Path,
) -> None:
    """An external symlink whose ``..`` re-enters the root is rejected.

    ``<ext_link>/../root/file`` resolves back inside the root, so the trusted
    boundary lands on the ``../root`` prefix. The leading ``ext_link`` symlink
    precedes that boundary and must still be detected under SKIP.
    """
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    ext_link = tmp_path / "ext_link"
    ext_link.symlink_to(sibling)
    (root / "file.jpg").touch()
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.SKIP,
    )
    candidate = ext_link / ".." / root.name / "file.jpg"

    assert_that(candidate.resolve()).is_equal_to((root / "file.jpg").resolve())
    with pytest.raises(SecurityError, match="symlink traversal is not permitted"):
        validator.validate_path(candidate)


@pytest.mark.parametrize(
    "policy",
    [SymlinkPolicy.SKIP, SymlinkPolicy.ERROR],
    ids=["policy=skip", "policy=error"],
)
def test_validate_path_rejects_every_traversed_symlink_unless_follow(
    root: Path,
    policy: SymlinkPolicy,
) -> None:
    """Under SKIP and ERROR alike, a symlink component raises SecurityError.

    Args:
        root: The allowed root directory.
        policy: The non-FOLLOW symlink policy under test.
    """
    real_dir = root / "real_dir"
    real_dir.mkdir()
    inner_real = real_dir / "inner_real"
    inner_real.mkdir()
    (inner_real / "photo.jpg").touch()
    inner_link = real_dir / "inner_link"
    inner_link.symlink_to(inner_real)
    outer_link = root / "outer_link"
    outer_link.symlink_to(real_dir)
    validator = PathValidator(allowed_roots=[root], symlink_policy=policy)

    with pytest.raises(
        SecurityError, match="symlink traversal is not permitted"
    ) as exc:
        validator.validate_path(outer_link / "inner_link" / "photo.jpg")

    assert_that(exc.value.context.details["symlinks"]).is_equal_to(
        [str(outer_link), str(outer_link / "inner_link")],
    )


def test_validate_path_accepts_path_under_symlinked_root_alias(
    tmp_path: Path,
) -> None:
    """A path using a symlinked root alias is accepted under SKIP policy."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    real_root = real_root.resolve()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root)
    validator = PathValidator(
        allowed_roots=[real_root],
        symlink_policy=SymlinkPolicy.SKIP,
    )

    result = validator.validate_path(alias_root / "photo.jpg")

    assert_that(result).is_equal_to(real_root / "photo.jpg")


def test_validate_path_reports_escape_for_out_of_root_symlinked_ancestor(
    root: Path,
    tmp_path: Path,
) -> None:
    """An out-of-root path via a symlinked ancestor reports an escape."""
    real_outside = tmp_path / "outside"
    real_outside.mkdir()
    link_outside = tmp_path / "outside_link"
    link_outside.symlink_to(real_outside)
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.SKIP,
    )

    with pytest.raises(SecurityError, match="escapes the allowed roots"):
        validator.validate_path(link_outside / "file.jpg")


def test_validate_path_follows_symlink_to_target_inside_root(root: Path) -> None:
    """Under FOLLOW policy, a symlink to an in-root target resolves."""
    real = root / "real.jpg"
    real.touch()
    link = root / "link.jpg"
    link.symlink_to(real)
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.FOLLOW,
    )

    assert_that(validator.validate_path(link)).is_equal_to(real.resolve())


def test_validate_path_rejects_symlink_escaping_root_under_follow(
    root: Path,
    tmp_path: Path,
) -> None:
    """Under FOLLOW policy, a symlink whose target escapes is rejected."""
    outside = tmp_path / "outside.jpg"
    outside.touch()
    link = root / "escape.jpg"
    link.symlink_to(outside)
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.FOLLOW,
    )

    with pytest.raises(SecurityError, match="escapes the allowed roots"):
        validator.validate_path(link)


def test_is_within_roots_reports_containment(root: Path, tmp_path: Path) -> None:
    """is_within_roots reflects whether a path resolves inside a root."""
    validator = PathValidator(allowed_roots=[root])

    assert_that(validator.is_within_roots(root / "a.jpg")).is_true()
    assert_that(validator.is_within_roots(tmp_path / "b.jpg")).is_false()


def test_validator_requires_at_least_one_root() -> None:
    """Constructing a validator with no roots raises SecurityError."""
    empty: Iterator[Path] = iter(())

    with pytest.raises(SecurityError, match="at least one allowed root"):
        PathValidator(allowed_roots=empty)


def test_allowed_roots_and_policy_are_exposed(root: Path) -> None:
    """The validator exposes its resolved roots and symlink policy."""
    validator = PathValidator(
        allowed_roots=[root],
        symlink_policy=SymlinkPolicy.ERROR,
    )

    assert_that(validator.allowed_roots).is_equal_to((root,))
    assert_that(validator.symlink_policy).is_equal_to(SymlinkPolicy.ERROR)


def test_validator_defaults_to_skip_policy(root: Path) -> None:
    """The default symlink policy is SKIP."""
    validator = PathValidator(allowed_roots=[root])

    assert_that(validator.symlink_policy).is_same_as(SymlinkPolicy.SKIP)


def test_security_symlink_policy_is_models_enum() -> None:
    """winnow.security re-exports the single SymlinkPolicy from winnow.models."""
    assert_that(winnow.security.SymlinkPolicy).is_same_as(
        winnow.models.enums.SymlinkPolicy,
    )
