"""Tests for configuration schema serialization helpers."""

from __future__ import annotations

from pathlib import Path

from assertpy import assert_that
from ruamel.yaml import YAML

from winnow.config.schema import render_config_yaml
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
