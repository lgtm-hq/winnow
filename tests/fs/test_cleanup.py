"""Tests for shared filesystem cleanup and tombstone-restore helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.fs import _cleanup as cleanup_module
from winnow.fs import transaction as transaction_module
from winnow.fs._cleanup import restore_tombstone


def test_restore_tombstone_missing_tombstone_is_noop(tmp_path: Path) -> None:
    """A missing tombstone leaves the destination untouched."""
    destination = tmp_path / "settings.yaml"
    destination.write_text("current\n", encoding="utf-8")

    restore_tombstone(
        tombstone=tmp_path / ".settings.yaml.missing.tmp",
        destination=destination,
    )

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("current\n")


def test_restore_tombstone_recreates_missing_destination_parent(
    tmp_path: Path,
) -> None:
    """Tombstone restoration recreates a destination parent that vanished."""
    destination = tmp_path / "nested" / "settings.yaml"
    tombstone = tmp_path / ".settings.yaml.tomb.tmp"
    tombstone.write_text("old\n", encoding="utf-8")

    restore_tombstone(tombstone=tombstone, destination=destination)

    assert_that(destination.read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that(tombstone.exists()).is_false()


def test_restore_tombstone_aggregates_failures_and_raises_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secondary restore failures are attached as notes to the first failure."""
    destination = tmp_path / "profile"
    destination.mkdir()
    (destination / "keep.txt").write_text("data\n", encoding="utf-8")
    tombstone = tmp_path / ".profile.tomb.tmp"
    tombstone.write_text("old\n", encoding="utf-8")

    def fail_remove(path: Path) -> None:
        """Raise a deterministic destination removal failure."""
        del path
        raise OSError("destination removal failed")

    monkeypatch.setattr(cleanup_module, "remove_path", fail_remove)

    with pytest.raises(OSError, match="destination removal failed") as exc_info:
        restore_tombstone(tombstone=tombstone, destination=destination)

    notes = getattr(exc_info.value, "__notes__", [])
    assert_that(notes).is_length(1)
    assert_that(notes[0]).starts_with("tombstone restore failed:")
    assert_that(tombstone.read_text(encoding="utf-8")).is_equal_to("old\n")
    assert_that((destination / "keep.txt").exists()).is_true()


def test_restore_tombstone_raises_replace_failure_without_notes(
    tmp_path: Path,
) -> None:
    """A lone replace failure is raised directly without secondary notes."""
    destination = tmp_path / "missing-parent-file" / "nested"
    tombstone = tmp_path / ".nested.tomb.tmp"
    tombstone.write_text("old\n", encoding="utf-8")
    (tmp_path / "missing-parent-file").write_text("blocker\n", encoding="utf-8")

    with pytest.raises(OSError) as exc_info:
        restore_tombstone(tombstone=tombstone, destination=destination)

    assert_that(getattr(exc_info.value, "__notes__", [])).is_empty()
    assert_that(tombstone.read_text(encoding="utf-8")).is_equal_to("old\n")


def test_transaction_delegates_to_shared_cleanup_helpers() -> None:
    """The transaction entry point binds the shared helper implementations."""
    assert_that(transaction_module._cleanup_path).is_same_as(
        cleanup_module.cleanup_path,
    )
    assert_that(transaction_module._missing_directories).is_same_as(
        cleanup_module.missing_directories,
    )
    assert_that(transaction_module._restore_tombstone).is_same_as(
        cleanup_module.restore_tombstone,
    )
    assert_that(transaction_module._run_cleanups).is_same_as(
        cleanup_module.run_cleanups,
    )
