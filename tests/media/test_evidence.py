"""Tests for evidence collection (#394)."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from assertpy import assert_that

import winnow.media.evidence as evidence_module
from winnow import __version__
from winnow.media import DetectionResult, TypeSignal, detect
from winnow.media.evidence import COLLECTOR, EvidenceReader, collect_evidence
from winnow.models.evidence import (
    Claim,
    EvidenceSet,
    Observation,
    ParseStatus,
    Precision,
    ReadIssue,
    SourceKind,
    TimeValue,
)
from winnow.models.media import MediaType

ReadFunction = Callable[[Path], None]


@dataclass
class FakeReader:
    """A reader that reports fixed software names and may act on the file.

    Args:
        name: Reader name.
        values: Values to report, one ``software`` observation each, in
            order.
        action: Run on the file before reporting, to fail or change it.
        calls: Paths read, in order.
        detections: Detection results received, in order.
    """

    name: str
    values: tuple[str, ...] = ()
    action: ReadFunction | None = None
    calls: list[Path] = field(default_factory=list)
    detections: list[DetectionResult] = field(default_factory=list)

    def version(self) -> str:
        """Return the fake version.

        Returns:
            ``"1.0"``.
        """
        return "1.0"

    def read(
        self,
        path: Path,
        *,
        detection: DetectionResult,
    ) -> tuple[list[Observation], list[ReadIssue]]:
        """Report one observation per value.

        Args:
            path: The file.
            detection: The file's detection result.

        Returns:
            The observations and no issues.
        """
        self.calls.append(path)
        self.detections.append(detection)
        if self.action is not None:
            self.action(path)
        observations = [
            Observation(
                id="",
                claim=Claim.SOFTWARE,
                source=SourceKind.EXIF,
                locator=f"IFD0:Software#{index}",
                origin=path,
                raw=value,
                value=value,
                status=ParseStatus.OK,
                family="exif",
                reader=self.name,
                reader_version=self.version(),
            )
            for index, value in enumerate(self.values)
        ]
        return observations, []


@dataclass
class MtimeReader:
    """A reader that reports the file's mtime as a UTC-by-spec value."""

    name: str = "fs-test"

    def version(self) -> str:
        """Return the fake version.

        Returns:
            ``"1.0"``.
        """
        return "1.0"

    def read(
        self,
        path: Path,
        *,
        detection: DetectionResult,
    ) -> tuple[list[Observation], list[ReadIssue]]:
        """Report the mtime without consulting the host timezone.

        Args:
            path: The file.
            detection: The file's detection result.

        Returns:
            One ``file_modified_time`` observation and no issues.
        """
        mtime_ns = path.stat().st_mtime_ns
        wall = datetime(1970, 1, 1) + timedelta(microseconds=mtime_ns // 1000)
        observation = Observation(
            id="",
            claim=Claim.FILE_MODIFIED_TIME,
            source=SourceKind.FILESYSTEM,
            locator="fs:st_mtime_ns",
            origin=path,
            raw=str(mtime_ns),
            value=TimeValue(
                wall=wall,
                offset_minutes=0,
                offset_origin="utc-by-spec",
                precision=Precision.SUBSECOND,
            ),
            status=ParseStatus.OK,
            family="fs",
            reader=self.name,
            reader_version=self.version(),
        )
        return [observation], []


@pytest.fixture
def media_file(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Copy the committed HEIC fixture to a scratch path.

    Returns:
        A writable copy of ``dated.heic``.
    """
    target = tmp_path / "dated.heic"
    shutil.copyfile(fixtures_dir / "dated.heic", target)
    return target


@pytest.fixture
def set_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Switch the process timezone, restoring it afterwards.

    Yields:
        A function that sets ``TZ`` and applies it with ``time.tzset``.
    """

    def apply(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield apply
    monkeypatch.undo()
    time.tzset()


def _raise(path: Path) -> None:
    """Fail like a broken backend.

    Args:
        path: The file.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError(f"backend crashed on {path.name}")


def _append(path: Path) -> None:
    """Append a byte to the file.

    Args:
        path: The file.
    """
    with path.open("ab") as handle:
        handle.write(b"\0")


def _rewrite_keeping_size_and_mtime(path: Path) -> None:
    """Rewrite the content with equal size and restore the mtime.

    The rewrite repeats until the inode change time moves, so the test does
    not depend on the filesystem's timestamp granularity.

    Args:
        path: The file.
    """
    original = path.stat()
    content = path.read_bytes()
    deadline = time.monotonic() + 5
    while True:
        path.write_bytes(bytes(byte ^ 0xFF for byte in content))
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        if path.stat().st_ctime_ns != original.st_ctime_ns:
            return
        if time.monotonic() > deadline:
            pytest.fail("ctime did not change within 5 seconds")


def _ids(evidence: EvidenceSet, *, reader: str) -> list[str]:
    """Return the IDs of one reader's observations.

    Args:
        evidence: The evidence set.
        reader: The reader name.

    Returns:
        The IDs, in collection order.
    """
    return [o.id for o in evidence.observations if o.reader == reader]


def test_collect_evidence_records_type_signals(fixtures_dir: Path) -> None:
    """``dated.heic`` yields image type evidence from ftyp and extension."""
    evidence = collect_evidence(fixtures_dir / "dated.heic", readers=())

    media_types = {
        (o.source, o.value)
        for o in evidence.observations
        if o.claim is Claim.MEDIA_TYPE and o.status is ParseStatus.OK
    }
    containers = {
        o.locator: o.value for o in evidence.observations if o.claim is Claim.CONTAINER
    }
    assert_that(media_types).contains(
        (SourceKind.FTYP, "image"),
        (SourceKind.EXTENSION, "image"),
    )
    assert_that(containers).contains_entry(
        {"detect:container": "iso-bmff"},
        {"ftyp:major_brand": "heic"},
    )
    assert_that(evidence.issues).is_empty()


def test_collect_evidence_keeps_every_signal_in_order(media_file: Path) -> None:
    """Every detection signal is kept in order, the deciding one included."""
    detection = detect(media_file)

    evidence = collect_evidence(media_file, readers=(), detection=detection)

    type_observations = [
        o for o in evidence.observations if o.claim is Claim.MEDIA_TYPE
    ]
    assert_that([o.raw for o in type_observations]).is_equal_to(
        [signal.detail for signal in detection.signals],
    )
    assert_that([o.locator for o in type_observations]).does_not_contain_duplicates()
    deciding = [o for o in type_observations if (o.raw or "").startswith("decision=")]
    assert_that(deciding).is_length(1)
    assert_that(deciding[0].source).is_equal_to(SourceKind.FTYP)
    empty = [o for o in type_observations if o.status is ParseStatus.EMPTY]
    assert_that([o.value for o in empty]).is_not_empty().contains_only(None)
    assert_that({o.reader for o in evidence.observations}).is_equal_to({COLLECTOR})
    assert_that({o.reader_version for o in evidence.observations}).is_equal_to(
        {__version__},
    )
    brands = [
        o.value
        for o in evidence.observations
        if o.locator.startswith("ftyp:compatible_brands[")
    ]
    assert_that(brands).is_equal_to(list(detection.compatible_brands))


def test_collect_evidence_reuses_given_detection(
    media_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A given detection result is used; the file is not detected again."""
    detection = detect(media_file)
    calls: list[Path] = []

    def spy(path: Path, **kwargs: object) -> DetectionResult:
        calls.append(path)
        return detection

    monkeypatch.setattr(evidence_module, "detect", spy)

    given = collect_evidence(media_file, readers=(), detection=detection)
    assert_that(calls).is_empty()

    detected = collect_evidence(media_file, readers=())
    assert_that(calls).is_equal_to([media_file])
    assert_that(detected).is_equal_to(given)


def test_collect_evidence_passes_detection_to_readers(media_file: Path) -> None:
    """Every reader receives the same detection result."""
    first = FakeReader(name="first")
    second = FakeReader(name="second")
    readers: list[EvidenceReader] = [first, second]
    detection = detect(media_file)

    collect_evidence(media_file, readers=readers, detection=detection)

    assert_that(first.detections).is_length(1)
    assert_that(first.detections[0]).is_same_as(detection)
    assert_that(second.detections).is_length(1)
    assert_that(second.detections[0]).is_same_as(detection)


def test_collect_evidence_isolates_failing_reader(media_file: Path) -> None:
    """A reader that raises gives one failed issue; the others still report."""
    readers = [
        FakeReader(name="first", values=("Camera 1.0",)),
        FakeReader(name="broken", action=_raise),
        FakeReader(name="last", values=("Editor 2.0", "Editor 2.0")),
    ]

    evidence = collect_evidence(media_file, readers=readers)

    assert_that(evidence.issues).is_length(1)
    issue = evidence.issues[0]
    assert_that(issue.reader).is_equal_to("broken")
    assert_that(issue.status).is_equal_to("failed")
    assert_that(issue.message).contains("RuntimeError", "backend crashed")
    software = [o for o in evidence.observations if o.claim is Claim.SOFTWARE]
    assert_that([(o.reader, o.value) for o in software]).is_equal_to(
        [
            ("first", "Camera 1.0"),
            ("last", "Editor 2.0"),
            ("last", "Editor 2.0"),
        ],
    )
    assert_that(software[1].id).is_not_equal_to(software[2].id)


def test_collect_evidence_assigns_ids_after_types(media_file: Path) -> None:
    """Type evidence comes first, then readers in order, each with an ID."""
    evidence = collect_evidence(
        media_file,
        readers=[FakeReader(name="a", values=("A",))],
    )

    claims = [o.claim for o in evidence.observations]
    assert_that(claims[-1]).is_equal_to(Claim.SOFTWARE)
    assert_that(claims[:-1]).does_not_contain(Claim.SOFTWARE)
    ids = [o.id for o in evidence.observations]
    assert_that(ids).does_not_contain("").does_not_contain_duplicates()


def test_collect_evidence_ids_ignore_reader_order(media_file: Path) -> None:
    """Running readers in another order changes positions, not IDs."""
    first = FakeReader(name="a", values=("A",))
    second = FakeReader(name="b", values=("B",))

    forward = collect_evidence(media_file, readers=[first, second])
    backward = collect_evidence(media_file, readers=[second, first])

    assert_that(_ids(forward, reader="a")).is_equal_to(_ids(backward, reader="a"))
    assert_that(_ids(forward, reader="b")).is_equal_to(_ids(backward, reader="b"))
    assert_that([o.id for o in forward.observations]).is_not_equal_to(
        [o.id for o in backward.observations],
    )


def test_collect_evidence_detects_change_during_read(media_file: Path) -> None:
    """A file that grows while a reader runs gives a file-changed issue."""
    evidence = collect_evidence(
        media_file,
        readers=[FakeReader(name="appender", action=_append)],
    )

    assert_that([i.status for i in evidence.issues]).is_equal_to(["file-changed"])
    assert_that(evidence.issues[0].message).contains("size")


def test_collect_evidence_detects_equal_size_rewrite(media_file: Path) -> None:
    """An equal-size rewrite with the mtime restored is caught by ctime."""
    before = media_file.stat()

    evidence = collect_evidence(
        media_file,
        readers=[FakeReader(name="rewriter", action=_rewrite_keeping_size_and_mtime)],
    )

    after = media_file.stat()
    assert_that(after.st_size).is_equal_to(before.st_size)
    assert_that(after.st_mtime_ns).is_equal_to(before.st_mtime_ns)
    assert_that([i.status for i in evidence.issues]).is_equal_to(["file-changed"])
    assert_that(evidence.issues[0].message).contains("ctime_ns")
    assert_that(evidence.issues[0].message).does_not_contain("size", "mtime_ns")


def test_collect_evidence_file_vanishing_during_read(media_file: Path) -> None:
    """A file deleted while a reader runs gives a file-changed issue."""
    evidence = collect_evidence(
        media_file,
        readers=[FakeReader(name="deleter", action=Path.unlink)],
    )

    assert_that([i.status for i in evidence.issues]).is_equal_to(["file-changed"])
    assert_that(evidence.issues[0].message).contains("vanished")


def test_collect_evidence_vanished_file_is_unreadable(tmp_path: Path) -> None:
    """A deleted file is unreadable, even when its name reads as a date."""
    path = tmp_path / "IMG_20190704_101010.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    path.unlink()
    reader = FakeReader(name="filename", values=("2019-07-04",))

    evidence = collect_evidence(path, readers=[reader])

    assert_that(reader.calls).is_empty()
    assert_that(evidence.snapshot).is_none()
    assert_that(evidence.observations).is_empty()
    assert_that([i.status for i in evidence.issues]).is_equal_to(["unreadable"])
    assert_that(evidence.issues[0].reader).is_equal_to(COLLECTOR)
    assert_that(evidence.issues[0].message).contains("FileNotFoundError")


def test_collect_evidence_directory_is_unreadable(tmp_path: Path) -> None:
    """A directory is not a file: unreadable, with its stat kept."""
    reader = FakeReader(name="any", values=("x",))

    evidence = collect_evidence(tmp_path, readers=[reader])

    assert_that(reader.calls).is_empty()
    assert_that(evidence.snapshot).is_not_none()
    assert_that(evidence.observations).is_empty()
    assert_that([i.status for i in evidence.issues]).is_equal_to(["unreadable"])


def test_collect_evidence_container_without_decision_signal(
    media_file: Path,
) -> None:
    """A container with no marked deciding signal is recorded as signature."""
    detection = DetectionResult(
        media_type=MediaType.IMAGE,
        container="jpeg",
        major_brand=None,
        signals=(
            TypeSignal(source="extension", detail="suffix=.heic", media_type=None),
        ),
        extension_media_type=None,
        agrees_with_extension=None,
    )

    evidence = collect_evidence(media_file, readers=(), detection=detection)

    containers = [o for o in evidence.observations if o.claim is Claim.CONTAINER]
    assert_that([(o.source, o.value) for o in containers]).is_equal_to(
        [(SourceKind.SIGNATURE, "jpeg")],
    )


def test_collect_evidence_without_container_records_none(media_file: Path) -> None:
    """No container is recorded when detection established none."""
    detection = DetectionResult(
        media_type=MediaType.VIDEO,
        container=None,
        major_brand=None,
        signals=(
            TypeSignal(
                source="override",
                detail="decision=override suffix=.heic",
                media_type=MediaType.VIDEO,
            ),
        ),
        extension_media_type=MediaType.IMAGE,
        agrees_with_extension=False,
    )

    evidence = collect_evidence(media_file, readers=(), detection=detection)

    assert_that(
        [(o.claim, o.source, o.value) for o in evidence.observations]
    ).is_equal_to(
        [(Claim.MEDIA_TYPE, SourceKind.OVERRIDE, "video")],
    )


def test_collect_evidence_independent_of_tz(
    media_file: Path,
    set_tz: Callable[[str], None],
) -> None:
    """The same file collects to identical JSON in any host timezone."""
    os.utime(media_file, ns=(1_562_231_410_123_456_789, 1_562_231_410_123_456_789))
    readers = [MtimeReader()]

    set_tz("UTC")
    utc = collect_evidence(media_file, readers=readers).model_dump_json()
    set_tz("Asia/Tokyo")
    tokyo = collect_evidence(media_file, readers=readers).model_dump_json()

    assert_that(tokyo).is_equal_to(utc)
    assert_that(utc).contains('"wall":"2019-07-04T09:10:10.123456"')
