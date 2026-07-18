"""Configuration for filesystem backup helper behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BackupOptions:
    """Options controlling user-visible backup creation.

    Args:
        enabled: Whether destructive operations create persistent backups.
        directory: Optional directory where backups are stored. When unset, backups
            are stored in a ``.winnow-backups`` directory next to the target path.
        suffix: Suffix appended to generated backup names.
    """

    enabled: bool = True
    directory: Path | None = None
    suffix: str = ".bak"


def coerce_backup_options(backup: bool | BackupOptions) -> BackupOptions:
    """Normalize backup configuration input.

    Args:
        backup: Backup flag or full backup options.

    Returns:
        Normalized backup options.
    """
    if isinstance(backup, BackupOptions):
        return backup
    return BackupOptions(enabled=backup)
