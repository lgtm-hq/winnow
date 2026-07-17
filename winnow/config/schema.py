"""Schema and serialization helpers for Winnow configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import cast

from winnow.models.config import WinnowConfig


def default_config() -> WinnowConfig:
    """Return a validated default Winnow configuration.

    Returns:
        New default configuration model.
    """
    return WinnowConfig()


def default_config_data(config: WinnowConfig | None = None) -> dict[str, object]:
    """Return JSON-serializable configuration data.

    Args:
        config: Configuration to serialize, or a default configuration.

    Returns:
        Dictionary suitable for YAML generation.
    """
    active_config = config if config is not None else default_config()
    return cast(
        "dict[str, object]",
        active_config.model_dump(mode="json", exclude_none=True),
    )


def config_json_schema() -> dict[str, object]:
    """Return the Pydantic-generated JSON schema for Winnow config.

    Returns:
        JSON schema describing :class:`winnow.models.config.WinnowConfig`.
    """
    return cast("dict[str, object]", WinnowConfig.model_json_schema())


def render_config_yaml(
    config: WinnowConfig | Mapping[str, object] | None = None,
) -> str:
    """Render configuration data as a YAML document.

    Args:
        config: Configuration model or mapping to render. Defaults are used when
            omitted.

    Returns:
        YAML document text ending with a newline.
    """
    if config is None:
        data = default_config_data()
    elif isinstance(config, WinnowConfig):
        data = default_config_data(config)
    else:
        data = dict(config)
    return "\n".join(_render_mapping(data)).rstrip() + "\n"


def _render_mapping(data: Mapping[str, object], indent: int = 0) -> list[str]:
    """Render a mapping as indented YAML lines.

    Args:
        data: Mapping to serialize.
        indent: Current indentation width.

    Returns:
        Serialized YAML lines.
    """
    lines: list[str] = []
    prefix = " " * indent
    for key, value in data.items():
        if isinstance(value, Mapping):
            lines.append(f"{prefix}{key}:")
            lines.extend(_render_mapping(value, indent=indent + 2))
        elif isinstance(value, list):
            lines.extend(_render_list(key=str(key), values=value, indent=indent))
        else:
            lines.append(f"{prefix}{key}: {_format_scalar(value)}")
    return lines


def _render_list(key: str, values: list[object], indent: int) -> list[str]:
    """Render a list value as YAML lines.

    Args:
        key: Mapping key for the list.
        values: List values to render.
        indent: Current indentation width.

    Returns:
        Serialized YAML lines.
    """
    prefix = " " * indent
    if not values:
        return [f"{prefix}{key}: []"]

    lines = [f"{prefix}{key}:"]
    item_prefix = " " * (indent + 2)
    for value in values:
        if isinstance(value, Mapping):
            lines.append(f"{item_prefix}-")
            lines.extend(_render_mapping(value, indent=indent + 4))
        else:
            lines.append(f"{item_prefix}- {_format_scalar(value)}")
    return lines


def _format_scalar(value: object) -> str:
    """Format a scalar value as YAML-compatible text.

    Args:
        value: Scalar value to serialize.

    Returns:
        YAML scalar representation.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, Enum):
        return json.dumps(value.value)
    if isinstance(value, Path):
        return json.dumps(str(value))
    return json.dumps(str(value))
