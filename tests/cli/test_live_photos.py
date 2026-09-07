"""Tests for the ``winnow live-photos`` command."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.classify import LivePhotoPair, LivePhotoScan
from winnow.cli import main

# ``winnow.cli.live_photos`` names the Click command on the package, so the
# module must be fetched explicitly to patch its ``detect_live_photos`` binding.
_MODULE = import_module("winnow.cli.live_photos")
# Short fake root so table cells are not folded across lines in assertions.
_ROOT = Path("lib")


def _scan(root: Path) -> LivePhotoScan:
    """Build a canned scan with one verified pair, one unverified, and orphans.

    Args:
        root: Directory the fake paths live under.

    Returns:
        A :class:`LivePhotoScan` with deterministic content.
    """
    return LivePhotoScan(
        pairs=(
            LivePhotoPair(
                still=root / "IMG_0001.HEIC",
                video=root / "IMG_0001.MOV",
                content_identifier="ABC-123",
                verified=True,
            ),
            LivePhotoPair(
                still=root / "IMG_0002.JPG",
                video=root / "IMG_0002.MOV",
                content_identifier=None,
                verified=False,
            ),
        ),
        unpaired_stills=(root / "lonely.jpg",),
        unpaired_videos=(root / "orphan.mov",),
    )


@pytest.fixture
def stub_detect(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Monkeypatch ``detect_live_photos`` and record the arguments it receives.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A dictionary populated with the ``directory`` and ``recursive`` values
        the command forwards to the detector.
    """
    calls: dict[str, object] = {}

    def fake_detect(directory: Path, *, recursive: bool = True) -> LivePhotoScan:
        """Record the call and return the canned scan.

        Args:
            directory: Directory the command asked to scan.
            recursive: Recursion flag the command forwarded.

        Returns:
            The canned scan rooted at ``_ROOT``.
        """
        calls["directory"] = directory
        calls["recursive"] = recursive
        return _scan(_ROOT)

    monkeypatch.setattr(_MODULE, "detect_live_photos", fake_detect)
    return calls


def test_help_lists_options() -> None:
    """``live-photos --help`` exits 0 and lists the documented options."""
    result = CliRunner().invoke(main, ["live-photos", "--help"])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("--unpaired", "--recursive", "--format")


def test_table_output_lists_pairs(
    tmp_path: Path,
    stub_detect: dict[str, object],
) -> None:
    """The default table contains both paths of each pair."""
    result = CliRunner().invoke(main, ["--no-color", "live-photos", str(tmp_path)])

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("IMG_0001.HEIC", "IMG_0001.MOV", "ABC-123")
    assert_that(result.output).contains("IMG_0002.JPG", "IMG_0002.MOV")
    assert_that(result.output).does_not_contain("lonely.jpg")
    assert_that(stub_detect["directory"]).is_equal_to(tmp_path)
    assert_that(stub_detect["recursive"]).is_true()


def test_no_recursive_is_forwarded(
    tmp_path: Path,
    stub_detect: dict[str, object],
) -> None:
    """``--no-recursive`` is passed through to the detector."""
    result = CliRunner().invoke(
        main,
        ["live-photos", "--no-recursive", str(tmp_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(stub_detect["recursive"]).is_false()


def test_unpaired_lists_orphans(
    tmp_path: Path,
    stub_detect: dict[str, object],
) -> None:
    """``--unpaired`` lists orphans with their kind instead of pairs."""
    result = CliRunner().invoke(
        main,
        ["--no-color", "live-photos", "--unpaired", str(tmp_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    assert_that(result.output).contains("lonely.jpg", "orphan.mov", "still", "video")
    assert_that(result.output).does_not_contain("IMG_0001.HEIC")


def test_json_output_round_trips_scan(
    tmp_path: Path,
    stub_detect: dict[str, object],
) -> None:
    """``--format json`` emits the scan as JSON with string paths."""
    result = CliRunner().invoke(
        main,
        ["live-photos", "--format", "json", str(tmp_path)],
    )

    assert_that(result.exit_code).is_equal_to(0)
    payload = json.loads(result.output)
    assert_that(payload["pairs"]).is_length(2)
    assert_that(payload["pairs"][0]).is_equal_to(
        {
            "still": str(_ROOT / "IMG_0001.HEIC"),
            "video": str(_ROOT / "IMG_0001.MOV"),
            "content_identifier": "ABC-123",
            "verified": True,
        },
    )
    assert_that(payload["pairs"][1]["verified"]).is_false()
    assert_that(payload["unpaired_stills"]).is_equal_to([str(_ROOT / "lonely.jpg")])
    assert_that(payload["unpaired_videos"]).is_equal_to([str(_ROOT / "orphan.mov")])


@pytest.mark.parametrize("output_format", ["csv", "markdown"])
def test_unsupported_format_is_usage_error(
    tmp_path: Path,
    stub_detect: dict[str, object],
    output_format: str,
) -> None:
    """Formats other than table and json exit 2 without scanning."""
    result = CliRunner().invoke(
        main,
        ["live-photos", "--format", output_format, str(tmp_path)],
    )

    assert_that(result.exit_code).is_equal_to(2)
    assert_that(result.output).contains("format not supported by live-photos")
    assert_that(stub_detect).is_empty()


def test_missing_directory_is_usage_error(tmp_path: Path) -> None:
    """A non-existent DIRECTORY is rejected by Click with exit code 2."""
    result = CliRunner().invoke(main, ["live-photos", str(tmp_path / "nope")])

    assert_that(result.exit_code).is_equal_to(2)
