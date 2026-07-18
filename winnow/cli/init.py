"""``winnow init`` command that guides first-run configuration setup."""

from __future__ import annotations

from pathlib import Path

import click

from winnow.cli.standards import config_path_option
from winnow.config import (
    cwd_config_path,
    generate_default_config,
    set_config_value,
)
from winnow.exceptions import ConfigError
from winnow.models.enums import HashAlgorithm, SortOrder

__all__ = ["init"]

_HASH_CHOICES = tuple(algorithm.value for algorithm in HashAlgorithm)
_SORT_CHOICES = tuple(order.value for order in SortOrder)


@click.command(name="init")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing configuration without prompting.",
)
@config_path_option()
def init(
    *,
    force: bool,
    config_path: Path | None,
) -> None:
    """Create a configuration file through guided prompts.

    Args:
        force: Overwrite an existing configuration file without prompting.
        config_path: Explicit configuration file path.

    Raises:
        ClickException: If the configuration cannot be created.
    """
    target = config_path if config_path is not None else cwd_config_path()
    if target.exists() and not force:
        click.confirm(
            f"{target} already exists. Overwrite?",
            abort=True,
        )

    source_dir = click.prompt(
        "Media source directory (leave blank to skip)",
        default="",
        show_default=False,
    ).strip()
    hash_algorithm = click.prompt(
        "Hash algorithm",
        type=click.Choice(_HASH_CHOICES),
        default=HashAlgorithm.SHA256.value,
    )
    sort_order = click.prompt(
        "Sort order",
        type=click.Choice(_SORT_CHOICES),
        default=SortOrder.BY_QUALITY.value,
    )
    workers = click.prompt(
        "Worker threads",
        type=click.IntRange(min=1),
        default=1,
    )
    dry_run = click.confirm(
        "Enable dry-run mode? Commands will only preview changes until you "
        "set dry_run = false",
        default=True,
    )

    overrides: dict[str, object] = {
        "hash_algorithm": hash_algorithm,
        "sort_order": sort_order,
        "workers": workers,
        "dry_run": dry_run,
    }
    if source_dir:
        overrides["source_dirs"] = [source_dir]

    try:
        generate_default_config(config_path=target, overwrite=True)
        for key, value in overrides.items():
            set_config_value(key=key, value=value, config_path=target)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Created configuration at {target}")
