"""Schema and serialization helpers for Winnow configuration."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import cast

from ruamel.yaml import YAML

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


def config_digest(config: WinnowConfig) -> str:
    """Return a stable content digest of a configuration.

    Args:
        config: Configuration to fingerprint.

    Returns:
        SHA-256 hex digest of ``config.model_dump_json()``.
    """
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


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
    data: object
    if config is None:
        data = default_config_data()
    elif isinstance(config, WinnowConfig):
        data = default_config_data(config)
    else:
        data = _yaml_safe(config)

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    stream = StringIO()
    yaml.dump(data, stream)
    return stream.getvalue()


def _yaml_safe(value: object) -> object:
    """Convert domain values to types supported by the YAML serializer.

    Args:
        value: Value to normalize before serialization.

    Returns:
        A recursively normalized value.
    """
    if isinstance(value, Mapping):
        return {str(key): _yaml_safe(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_yaml_safe(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value
