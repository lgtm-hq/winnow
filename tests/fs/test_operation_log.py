"""Tests for :class:`OperationLog` serialization round-trips."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.fs import FileOperation, OperationLog
from winnow.fs.operations import OperationStatus

BARE = OperationLog(operation=FileOperation.MKDIR)
MOVE = OperationLog(
    operation=FileOperation.MOVE,
    source=Path("/src/a.txt"),
    destination=Path("/dst/a.txt"),
)
WITH_BACKUPS = OperationLog(
    operation=FileOperation.COPY,
    source=Path("/src/b.txt"),
    destination=Path("/dst/b.txt"),
    backups=(Path("/dst/b.txt.bak"),),
    status=OperationStatus.ROLLED_BACK,
)
WITH_CREATED = OperationLog(
    operation=FileOperation.MKDIR,
    destination=Path("/dst/x/y"),
    created_paths=(Path("/dst/x"), Path("/dst/x/y")),
)


@pytest.mark.parametrize(
    "log",
    [BARE, MOVE, WITH_BACKUPS, WITH_CREATED],
    ids=["bare", "move", "with_backups", "with_created_paths"],
)
def test_from_dict_round_trips_as_dict(log: OperationLog) -> None:
    """``from_dict(as_dict())`` rebuilds an equal log for every field shape."""
    assert_that(OperationLog.from_dict(log.as_dict())).is_equal_to(log)


def test_from_dict_defaults_missing_optional_keys() -> None:
    """Only ``operation`` is required; everything else falls back to defaults."""
    log = OperationLog.from_dict({"operation": "delete"})
    assert_that(log.operation).is_equal_to(FileOperation.DELETE)
    assert_that(log.status).is_equal_to(OperationStatus.APPLIED)
    assert_that(log.source).is_none()
    assert_that(log.backups).is_empty()
    assert_that(log.created_paths).is_empty()


def test_from_dict_without_operation_raises() -> None:
    """A payload lacking ``operation`` is rejected."""
    with pytest.raises(ValueError, match="operation"):
        OperationLog.from_dict({"status": "applied"})
