"""Tests for configuration schema serialization helpers."""

from __future__ import annotations

from pathlib import Path

from assertpy import assert_that
from ruamel.yaml import YAML

from winnow.config.schema import config_digest, render_config_yaml
from winnow.models.config import WinnowConfig
from winnow.models.enums import SymlinkPolicy


def test_render_config_yaml_handles_yaml_special_strings() -> None:
    """Verify YAML rendering quotes strings that look like YAML syntax."""
    rendered = render_config_yaml(
        {
            "mapping_like": "{not: a mapping}",
            "anchor_like": "*not-an-anchor",
            "paths": {"cache": Path("/home/user/winnow-cache")},
            "policies": [SymlinkPolicy.FOLLOW],
        },
    )

    loaded = YAML(typ="safe").load(rendered)

    assert_that(loaded["mapping_like"]).is_equal_to("{not: a mapping}")
    assert_that(loaded["anchor_like"]).is_equal_to("*not-an-anchor")
    assert_that(loaded["paths"]["cache"]).is_equal_to("/home/user/winnow-cache")
    assert_that(loaded["policies"]).is_equal_to(["follow"])


def test_config_digest_is_sha256_hex() -> None:
    """The digest of a configuration is a 64-character hex string."""
    digest = config_digest(WinnowConfig())

    assert_that(digest).is_length(64).matches(r"^[0-9a-f]{64}$")


def test_config_digest_is_stable_across_instances() -> None:
    """Two equal configurations produce the same digest."""
    assert_that(config_digest(WinnowConfig())).is_equal_to(
        config_digest(WinnowConfig()),
    )


def test_config_digest_changes_with_workers() -> None:
    """Changing a field such as ``workers`` changes the digest."""
    assert_that(config_digest(WinnowConfig(workers=2))).is_not_equal_to(
        config_digest(WinnowConfig()),
    )
