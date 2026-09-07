"""Tests for the Discovery pipeline step."""

from __future__ import annotations

import os
import stat as stat_module
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from winnow.models.config import OrganizeSettings, PathSettings, WinnowConfig
from winnow.models.enums import SymlinkPolicy
from winnow.models.media import MediaType
from winnow.models.pipeline import PipelineResult, PipelineStep, RunMetadata
from winnow.pipeline import (
    DiscoveryStep,
    PipelineContext,
    PipelineEvent,
    RunState,
    StepCompleted,
    StepIssue,
    StepProgress,
    StepStarted,
)

_WINDOWS = sys.platform.startswith("win")


class _RecordingEvents:
    """StepEvents fake that records every emitted event.

    Attributes:
        events: Every event passed to :meth:`emit`, in order.
    """

    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    def emit(self, event: PipelineEvent) -> None:
        """Append the event to the log.

        Args:
            event: The event to record.
        """
        self.events.append(event)


def _write_png(path: Path) -> None:
    """Write a tiny real PNG to ``path``, creating parent directories.

    Args:
        path: Destination file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (4, 3), (10, 20, 30, 128)).save(path)


def _write_jpeg(path: Path) -> None:
    """Write a tiny real JPEG to ``path``, creating parent directories.

    Args:
        path: Destination file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 3), (120, 60, 30)).save(path)


def _write_noise(path: Path) -> None:
    """Write a non-media text file to ``path``, creating parent directories.

    Args:
        path: Destination file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not media\n", encoding="utf-8")


@pytest.fixture
def media_tree(tmp_path: Path) -> Path:
    """Build a nested tree of media and non-media files.

    Layout (``*`` marks media)::

        root/
          b.jpg *          notes.txt
          .hidden/x.png    .winnow-backups/y.png    .winnow-staging/z.jpg
          a/
            z.png *        readme.txt
            deep/
              m.jpg *
              deeper/
                n.png *
          skipme/
            s.jpg *
          keep/
            skipfile.png * (excluded by pattern in the exclude test)
            k.jpg *

    Returns:
        The tree root.
    """
    root = tmp_path / "root"
    _write_jpeg(root / "b.jpg")
    _write_noise(root / "notes.txt")
    _write_png(root / ".hidden" / "x.png")
    _write_png(root / ".winnow-backups" / "y.png")
    _write_jpeg(root / ".winnow-staging" / "z.jpg")
    _write_png(root / "a" / "z.png")
    _write_noise(root / "a" / "readme.txt")
    _write_jpeg(root / "a" / "deep" / "m.jpg")
    _write_png(root / "a" / "deep" / "deeper" / "n.png")
    _write_jpeg(root / "skipme" / "s.jpg")
    _write_png(root / "keep" / "skipfile.png")
    _write_jpeg(root / "keep" / "k.jpg")
    return root


def _make_state(source: Path, *, events: _RecordingEvents | None = None) -> RunState:
    """Build a fresh RunState rooted at ``source``.

    Args:
        source: Directory the run reads from.
        events: Optional recording sink; defaults to the null sink.

    Returns:
        A run state with an empty result.
    """
    result = PipelineResult(
        run=RunMetadata(started_at=datetime.now(tz=UTC), winnow_version="0.0.0"),
    )
    if events is None:
        return RunState(source=source, destination=source / "out", result=result)
    return RunState(
        source=source,
        destination=source / "out",
        result=result,
        events=events,
    )


def _run(source: Path, *, config: WinnowConfig | None = None) -> RunState:
    """Run DiscoveryStep against ``source`` and return the resulting state.

    Args:
        source: Directory to discover.
        config: Configuration for the run; defaults to ``WinnowConfig()``.

    Returns:
        The state after the step ran.
    """
    state = _make_state(source)
    context = PipelineContext.from_config(config or WinnowConfig())
    DiscoveryStep().run(state, context=context)
    return state


def _follow_config() -> WinnowConfig:
    """Return a config whose symlink policy is FOLLOW.

    Returns:
        Configuration with ``follow_symlinks`` and ``symlink_policy`` in sync.
    """
    return WinnowConfig(follow_symlinks=True, symlink_policy=SymlinkPolicy.FOLLOW)


def _relative_paths(state: RunState) -> list[str]:
    """Return discovered paths relative to the resolved source root.

    Args:
        state: State produced by a discovery run.

    Returns:
        POSIX-style relative paths in discovery order.
    """
    root = state.source.resolve()
    return [file.path.relative_to(root).as_posix() for file in state.files]


def test_discovery_step_name() -> None:
    """The step identifies itself as DISCOVERY."""
    assert_that(DiscoveryStep().name).is_equal_to(PipelineStep.DISCOVERY)


def test_discovery_collects_media_in_sorted_order(media_tree: Path) -> None:
    """Files precede subdirectories, entries are name-sorted, noise is skipped."""
    state = _run(media_tree)

    assert_that(_relative_paths(state)).is_equal_to(
        [
            "b.jpg",
            "a/z.png",
            "a/deep/m.jpg",
            "a/deep/deeper/n.png",
            "keep/k.jpg",
            "keep/skipfile.png",
            "skipme/s.jpg",
        ],
    )
    assert_that(state.result.errors).is_empty()


def test_discovery_is_deterministic_across_runs(media_tree: Path) -> None:
    """Two runs over the same tree produce identical ordered paths."""
    first = _relative_paths(_run(media_tree))
    second = _relative_paths(_run(media_tree))

    assert_that(second).is_equal_to(first)


def test_discovery_populates_media_file_fields(media_tree: Path) -> None:
    """Each MediaFile carries the resolved path, type, extension and size."""
    state = _run(media_tree)
    jpeg = state.files[0]
    expected = (media_tree / "b.jpg").resolve()

    assert_that(jpeg.path).is_equal_to(expected)
    assert_that(jpeg.path.is_absolute()).is_true()
    assert_that(jpeg.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(jpeg.extension).is_equal_to(".jpg")
    assert_that(jpeg.size_bytes).is_equal_to(expected.stat().st_size)
    assert_that(jpeg.creation_date.tzinfo).is_not_none()
    assert_that(jpeg.metadata).is_none()


@pytest.mark.parametrize(
    ("max_depth", "expected"),
    [
        (0, ["b.jpg"]),
        (1, ["b.jpg", "a/z.png", "keep/k.jpg", "keep/skipfile.png", "skipme/s.jpg"]),
        (
            None,
            [
                "b.jpg",
                "a/z.png",
                "a/deep/m.jpg",
                "a/deep/deeper/n.png",
                "keep/k.jpg",
                "keep/skipfile.png",
                "skipme/s.jpg",
            ],
        ),
    ],
    ids=["depth=0", "depth=1", "depth=unlimited"],
)
def test_discovery_honours_max_depth(
    media_tree: Path,
    max_depth: int | None,
    expected: list[str],
) -> None:
    """max_depth limits how far below the root the walk descends."""
    config = WinnowConfig(organize=OrganizeSettings(max_depth=max_depth))

    state = _run(media_tree, config=config)

    assert_that(_relative_paths(state)).is_equal_to(expected)


def test_discovery_applies_exclude_patterns(media_tree: Path) -> None:
    """A pattern excludes matching directories (with descendants) and files."""
    config = WinnowConfig(paths=PathSettings(exclude_patterns=["skip*"]))

    state = _run(media_tree, config=config)

    assert_that(_relative_paths(state)).is_equal_to(
        ["b.jpg", "a/z.png", "a/deep/m.jpg", "a/deep/deeper/n.png", "keep/k.jpg"],
    )


def test_discovery_exclude_matches_relative_path(media_tree: Path) -> None:
    """Patterns are also matched against the root-relative POSIX path."""
    config = WinnowConfig(paths=PathSettings(exclude_patterns=["a/deep"]))

    state = _run(media_tree, config=config)

    assert_that(_relative_paths(state)).is_equal_to(
        ["b.jpg", "a/z.png", "keep/k.jpg", "keep/skipfile.png", "skipme/s.jpg"],
    )


@pytest.mark.skipif(_WINDOWS, reason="symlink creation needs privileges on Windows")
def test_discovery_follow_records_escaping_symlink(tmp_path: Path) -> None:
    """Under FOLLOW an escaping link is skipped and recorded as one issue."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    _write_png(root / "in.png")
    _write_png(outside / "out.png")
    (root / "escape.png").symlink_to(outside / "out.png")
    config = _follow_config()

    state = _run(root, config=config)

    assert_that(_relative_paths(state)).is_equal_to(["in.png"])
    assert_that(state.result.errors).is_length(1)
    assert_that(state.result.errors[0]).starts_with("discovery: ")
    assert_that(state.result.errors[0]).contains("escape.png")


@pytest.mark.skipif(_WINDOWS, reason="symlink creation needs privileges on Windows")
def test_discovery_follow_accepts_internal_symlink(tmp_path: Path) -> None:
    """Under FOLLOW a link whose target stays inside the root is collected."""
    root = tmp_path / "root"
    _write_png(root / "real" / "in.png")
    (root / "alias.png").symlink_to(root / "real" / "in.png")
    config = _follow_config()

    state = _run(root, config=config)

    assert_that(_relative_paths(state)).is_equal_to(["real/in.png", "real/in.png"])
    assert_that(state.result.errors).is_empty()


@pytest.mark.skipif(_WINDOWS, reason="symlink creation needs privileges on Windows")
def test_discovery_follow_does_not_loop_on_directory_symlink(tmp_path: Path) -> None:
    """A directory symlink pointing at an ancestor is visited at most once."""
    root = tmp_path / "root"
    _write_png(root / "sub" / "in.png")
    (root / "sub" / "loop").symlink_to(root)
    config = _follow_config()

    state = _run(root, config=config)

    assert_that(_relative_paths(state)).is_equal_to(["sub/in.png"])
    assert_that(state.result.errors).is_empty()


@pytest.mark.skipif(_WINDOWS, reason="symlink creation needs privileges on Windows")
def test_discovery_skip_ignores_symlinks_silently(tmp_path: Path) -> None:
    """Under SKIP every symlink is skipped without recording an issue."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    _write_png(root / "in.png")
    _write_png(outside / "out.png")
    (root / "escape.png").symlink_to(outside / "out.png")
    (root / "alias.png").symlink_to(root / "in.png")

    state = _run(root, config=WinnowConfig(symlink_policy=SymlinkPolicy.SKIP))

    assert_that(_relative_paths(state)).is_equal_to(["in.png"])
    assert_that(state.result.errors).is_empty()


@pytest.mark.skipif(_WINDOWS, reason="symlink creation needs privileges on Windows")
def test_discovery_error_policy_records_every_symlink(tmp_path: Path) -> None:
    """Under ERROR every symlink is skipped and recorded as an issue."""
    root = tmp_path / "root"
    _write_png(root / "in.png")
    (root / "alias.png").symlink_to(root / "in.png")

    state = _run(root, config=WinnowConfig(symlink_policy=SymlinkPolicy.ERROR))

    assert_that(_relative_paths(state)).is_equal_to(["in.png"])
    assert_that(state.result.errors).is_length(1)
    assert_that(state.result.errors[0]).starts_with("discovery: ")
    assert_that(state.result.errors[0]).contains("alias.png")


@pytest.mark.skipif(_WINDOWS, reason="chmod 000 has no effect on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions",
)
def test_discovery_records_unreadable_directory(tmp_path: Path) -> None:
    """An unreadable directory is recorded as an issue and the walk completes."""
    root = tmp_path / "root"
    locked = root / "locked"
    _write_png(root / "a.png")
    _write_png(locked / "hidden.png")
    _write_png(root / "z" / "after.png")
    locked.chmod(0)
    try:
        state = _run(root)
    finally:
        locked.chmod(stat_module.S_IRWXU)

    assert_that(_relative_paths(state)).is_equal_to(["a.png", "z/after.png"])
    assert_that(state.result.errors).is_length(1)
    assert_that(state.result.errors[0]).starts_with(f"discovery: {locked}: ")
    assert_that(PipelineStep.DISCOVERY).is_in(*state.result.steps_completed)


def test_discovery_records_missing_root(tmp_path: Path) -> None:
    """A missing source root yields no files and one recorded issue."""
    missing = tmp_path / "nope"

    state = _run(missing)

    assert_that(state.files).is_empty()
    assert_that(state.result.errors).is_length(1)
    assert_that(state.result.errors[0]).starts_with(f"discovery: {missing}: ")


def test_discovery_updates_counters_and_steps(media_tree: Path) -> None:
    """Counters, steps_completed and step_durations are filled on completion."""
    state = _run(media_tree)

    assert_that(state.result.total_files_scanned).is_equal_to(len(state.files))
    assert_that(state.result.total_files_scanned).is_equal_to(7)
    assert_that(state.result.steps_completed).is_equal_to([PipelineStep.DISCOVERY])
    assert_that(state.step_durations).contains_key(PipelineStep.DISCOVERY)
    assert_that(
        state.step_durations[PipelineStep.DISCOVERY]
    ).is_greater_than_or_equal_to(
        0.0,
    )


def test_discovery_emits_event_sequence(media_tree: Path) -> None:
    """A small run emits StepStarted, one final StepProgress, StepCompleted."""
    events = _RecordingEvents()
    state = _make_state(media_tree, events=events)

    DiscoveryStep().run(state, context=PipelineContext.from_config(WinnowConfig()))

    assert_that(events.events).is_length(3)
    started, progress, completed = events.events
    assert_that(started).is_equal_to(StepStarted(step=PipelineStep.DISCOVERY))
    assert_that(progress).is_equal_to(
        StepProgress(step=PipelineStep.DISCOVERY, current=7, total=None),
    )
    assert_that(completed).is_equal_to(
        StepCompleted(
            step=PipelineStep.DISCOVERY,
            duration_seconds=state.step_durations[PipelineStep.DISCOVERY],
        ),
    )


def test_discovery_emits_progress_every_hundred_files(tmp_path: Path) -> None:
    """StepProgress fires at every 100th accepted file plus once at the end."""
    root = tmp_path / "root"
    root.mkdir()
    source = root / "seed.png"
    _write_png(source)
    payload = source.read_bytes()
    for index in range(1, 150):
        (root / f"f{index:03d}.png").write_bytes(payload)
    events = _RecordingEvents()
    state = _make_state(root, events=events)

    DiscoveryStep().run(state, context=PipelineContext.from_config(WinnowConfig()))

    progress = [e for e in events.events if isinstance(e, StepProgress)]
    assert_that([e.current for e in progress]).is_equal_to([100, 150])


def test_discovery_issue_events_reach_sink(tmp_path: Path) -> None:
    """Recorded issues are also emitted as StepIssue events."""
    events = _RecordingEvents()
    state = _make_state(tmp_path / "missing", events=events)

    DiscoveryStep().run(state, context=PipelineContext.from_config(WinnowConfig()))

    issues = [e for e in events.events if isinstance(e, StepIssue)]
    assert_that(issues).is_length(1)
    assert_that(issues[0].step).is_equal_to(PipelineStep.DISCOVERY)
    assert_that(issues[0].path).is_equal_to(tmp_path / "missing")
