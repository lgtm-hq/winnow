"""Tests for evidence collection (#394)."""

from __future__ import annotations

import errno
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from assertpy import assert_that

import winnow.media._detect as detect_module
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

PathAction = Callable[[Path], None]


@dataclass
class FakeReader:
    """A reader that reports fixed software names and may act on the file.

    Args:
        name: Reader name.
        values: Values to report, one ``software`` observation each, in
            order.
        action: Run on the file before reporting, to fail or change it.
        origin: Report observations about this file instead of the one
            read, as a sidecar reader would.
        calls: Paths read, in order.
        detections: Detection results received, in order.
    """

    name: str
    values: tuple[str, ...] = ()
    action: PathAction | None = None
    origin: Path | None = None
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
                origin=self.origin or path,
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
            locator="fs:st_mtime_ns#0",
            origin=path,
            raw=str(mtime_ns),
            value=TimeValue(
                wall=wall,
                offset_minutes=0,
                offset_origin="utc-by-spec",
                precision=Precision.SUBSECOND,
                fraction_digits=9,
            ),
            status=ParseStatus.OK,
            family="fs",
            reader=self.name,
            reader_version=self.version(),
        )
        return [observation], []


@dataclass
class DetectSpy:
    """Count calls to ``detect``, optionally acting on the file first.

    Args:
        before: Run on the path before the real detection.
        calls: Paths detected, in order.
    """

    before: PathAction | None = None
    calls: list[Path] = field(default_factory=list)

    def __call__(self, path: Path, **kwargs: object) -> DetectionResult:
        """Record the call and run the real detection.

        Args:
            path: The file.
            **kwargs: Passed through (the registry).

        Returns:
            The real detection result.
        """
        self.calls.append(path)
        if self.before is not None:
            self.before(path)
        return detect(path)


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
def jpeg_file(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Copy the committed JPEG fixture to a scratch path.

    Returns:
        A writable copy of ``sample.jpg``.
    """
    target = tmp_path / "sample.jpg"
    shutil.copyfile(fixtures_dir / "sample.jpg", target)
    return target


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> DetectSpy:
    """Replace the collector's ``detect`` with a counting spy.

    Returns:
        The spy.
    """
    recorder = DetectSpy()
    monkeypatch.setattr(evidence_module, "detect", recorder)
    return recorder


@pytest.fixture
def failing_content_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the detector's content read fail with ``EIO``."""

    def broken(*args: object, **kwargs: object) -> object:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(detect_module, "_read_content", broken)


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


def _ids_by_raw(evidence: EvidenceSet) -> dict[str | None, str]:
    """Map each type observation's raw value to its ID.

    Args:
        evidence: The evidence set.

    Returns:
        Raw value to ID, for ``media_type`` observations.
    """
    return {o.raw: o.id for o in evidence.observations if o.claim is Claim.MEDIA_TYPE}


def _assert_unreadable(evidence: EvidenceSet, *, contains: str) -> None:
    """Assert a set is one unreadable issue and nothing else.

    Args:
        evidence: The evidence set.
        contains: Text the issue's message must contain.
    """
    assert_that(evidence.observations).is_empty()
    assert_that([i.status for i in evidence.issues]).is_equal_to(["unreadable"])
    assert_that(evidence.issues[0].reader).is_equal_to(COLLECTOR)
    assert_that(evidence.issues[0].message).contains(contains)


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
    assert_that(deciding[0].locator).is_equal_to("detect:ftyp:decision#0")
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


def test_collect_evidence_counts_repeated_fields(tmp_path: Path) -> None:
    """Repeats of one signal field get counters scoped to that field."""
    path = tmp_path / "x.heic"
    path.write_bytes(b"")
    detection = detect(path).model_copy(
        update={
            "signals": (
                TypeSignal(source="puremagic", detail=".mp4 a", media_type=None),
                TypeSignal(source="puremagic", detail=".mov b", media_type=None),
                TypeSignal(source="puremagic", detail=".mp4 c", media_type=None),
                TypeSignal(source="extension", detail="decision=x", media_type=None),
            ),
        },
    )

    evidence = collect_evidence(path, readers=(), detection=detection)

    assert_that([o.locator for o in evidence.observations]).is_equal_to(
        [
            "detect:puremagic:.mp4#0",
            "detect:puremagic:.mov#0",
            "detect:puremagic:.mp4#1",
            "detect:extension:decision#0",
        ],
    )


def test_collect_evidence_ids_survive_unrelated_signals(media_file: Path) -> None:
    """Removing advisory signals or reordering others keeps every other ID."""
    detection = detect(media_file)
    without_puremagic = detection.model_copy(
        update={
            "signals": tuple(s for s in detection.signals if s.source != "puremagic"),
        },
    )
    reversed_signals = detection.model_copy(
        update={"signals": tuple(reversed(detection.signals))},
    )

    full = _ids_by_raw(collect_evidence(media_file, readers=(), detection=detection))
    fewer = _ids_by_raw(
        collect_evidence(media_file, readers=(), detection=without_puremagic),
    )
    backward = _ids_by_raw(
        collect_evidence(media_file, readers=(), detection=reversed_signals),
    )

    assert_that(fewer["suffix=.heic"]).is_equal_to(full["suffix=.heic"])
    assert_that(full).contains_entry(*({raw: i} for raw, i in fewer.items()))
    assert_that(backward).is_equal_to(full)


def test_collect_evidence_reuses_given_detection(
    media_file: Path,
    spy: DetectSpy,
) -> None:
    """A detection of this file state is used; the file is not read again."""
    detection = detect(media_file)

    given = collect_evidence(media_file, readers=(), detection=detection)
    assert_that(spy.calls).is_empty()

    detected = collect_evidence(media_file, readers=())
    assert_that(spy.calls).is_equal_to([media_file])
    assert_that(detected).is_equal_to(given)


def test_collect_evidence_redetects_stale_detection(
    media_file: Path,
    spy: DetectSpy,
) -> None:
    """A detection of an older file state is not trusted: detect afresh."""
    old = detect(media_file)
    media_file.write_text("plain text now, not a HEIC\n")

    evidence = collect_evidence(media_file, readers=(), detection=old)

    assert_that(spy.calls).is_equal_to([media_file])
    sources = {o.source for o in evidence.observations}
    assert_that(sources).does_not_contain(SourceKind.FTYP)
    assert_that(evidence.issues).is_empty()


def test_collect_evidence_redetects_other_path(
    media_file: Path,
    tmp_path: Path,
    spy: DetectSpy,
) -> None:
    """A detection of another path is not trusted, even with equal content."""
    other = tmp_path / "copy.heic"
    shutil.copyfile(media_file, other)

    collect_evidence(media_file, readers=(), detection=detect(other))

    assert_that(spy.calls).is_equal_to([media_file])


def test_collect_evidence_redetects_hand_built_detection(
    media_file: Path,
    spy: DetectSpy,
) -> None:
    """A result with no snapshot is never trusted."""
    hand_built = DetectionResult(
        media_type=MediaType.VIDEO,
        container="iso-bmff",
        major_brand="isom",
        signals=(TypeSignal(source="ftyp", detail="decision=x", media_type=None),),
        extension_media_type=None,
        agrees_with_extension=None,
    )

    evidence = collect_evidence(media_file, readers=(), detection=hand_built)

    assert_that(spy.calls).is_equal_to([media_file])
    values = [o.value for o in evidence.observations if o.claim is Claim.CONTAINER]
    assert_that(values).contains("heic").does_not_contain("isom")


def test_collect_evidence_flags_change_before_detection(
    media_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection reading a newer file state than the snapshot is reported."""
    monkeypatch.setattr(evidence_module, "detect", DetectSpy(before=_append))

    evidence = collect_evidence(media_file, readers=())

    messages = [i.message for i in evidence.issues if i.status == "file-changed"]
    assert_that(messages).is_not_empty()
    assert_that(messages[0]).contains("before detection", "size")


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


def test_collect_evidence_rejects_foreign_origin(
    media_file: Path,
    tmp_path: Path,
) -> None:
    """Observations about another file are not stored; the reader is flagged."""
    sidecar = tmp_path / "dated.xmp"
    readers = [
        FakeReader(name="own", values=("A",)),
        FakeReader(name="sidecar", values=("B", "C"), origin=sidecar),
    ]

    evidence = collect_evidence(media_file, readers=readers)

    assert_that({o.origin for o in evidence.observations}).is_equal_to({media_file})
    assert_that(_ids(evidence, reader="own")).is_length(1)
    assert_that(_ids(evidence, reader="sidecar")).is_empty()
    assert_that([(i.reader, i.status) for i in evidence.issues]).is_equal_to(
        [("sidecar", "failed")],
    )
    assert_that(evidence.issues[0].message).contains("2 observation", "#402")


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
    assert_that(evidence.issues[0].message).contains("during read", "size")


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
    _assert_unreadable(evidence, contains="FileNotFoundError")


def test_collect_evidence_deleted_after_preflight_is_unreadable(
    jpeg_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file gone before detection opens it gives no filename-only evidence."""
    check = evidence_module._check_readable

    def check_then_delete(path: Path) -> None:
        check(path)
        path.unlink()

    monkeypatch.setattr(evidence_module, "_check_readable", check_then_delete)
    reader = FakeReader(name="any", values=("x",))

    evidence = collect_evidence(jpeg_file, readers=[reader])

    assert_that(reader.calls).is_empty()
    assert_that(evidence.snapshot).is_not_none()
    _assert_unreadable(evidence, contains="unavailable=content FileNotFoundError")


@pytest.mark.usefixtures("failing_content_read")
def test_collect_evidence_content_read_error_is_unreadable(jpeg_file: Path) -> None:
    """An I/O error at the detector's read gives no type or reader evidence."""
    reader = FakeReader(name="any", values=("x",))

    evidence = collect_evidence(jpeg_file, readers=[reader])

    assert_that(reader.calls).is_empty()
    _assert_unreadable(evidence, contains="unavailable=content OSError")


@pytest.mark.usefixtures("failing_content_read")
def test_collect_evidence_given_unread_detection_is_unreadable(
    jpeg_file: Path,
    spy: DetectSpy,
) -> None:
    """A given result whose content read failed is not adapted either."""
    unread = detect(jpeg_file)
    reader = FakeReader(name="any", values=("x",))

    evidence = collect_evidence(jpeg_file, readers=[reader], detection=unread)

    assert_that(unread.snapshot).is_none()
    assert_that(spy.calls).is_equal_to([jpeg_file])
    assert_that(reader.calls).is_empty()
    _assert_unreadable(evidence, contains="unavailable=content OSError")


def test_collect_evidence_directory_is_unreadable(tmp_path: Path) -> None:
    """A directory is not a file: unreadable, with its stat kept."""
    reader = FakeReader(name="any", values=("x",))

    evidence = collect_evidence(tmp_path, readers=[reader])

    assert_that(reader.calls).is_empty()
    assert_that(evidence.snapshot).is_not_none()
    _assert_unreadable(evidence, contains="not a regular file")


def test_collect_evidence_invalid_path_is_unreadable(tmp_path: Path) -> None:
    """A path no filesystem allows (an embedded NUL) is unreadable."""
    evidence = collect_evidence(tmp_path / "bad\0.jpg", readers=())

    assert_that(evidence.snapshot).is_none()
    _assert_unreadable(evidence, contains="ValueError")


def test_collect_evidence_non_utf8_file_name(
    tmp_path: Path,
    fixtures_dir: Path,
) -> None:
    """A non-UTF-8 file name collects and round-trips where it can exist.

    Linux allows any bytes in a name. macOS (APFS) refuses a name that is not
    UTF-8 with ``EILSEQ``, so there the test asserts that refusal instead;
    ``tests/models/test_evidence.py`` covers the encoding on every platform.
    """
    path = tmp_path / os.fsdecode(b"caf\xe9.jpg")
    try:
        shutil.copyfile(fixtures_dir / "sample.jpg", path)
    except OSError as exc:
        assert_that(sys.platform).is_equal_to("darwin")
        assert_that(exc.errno).is_equal_to(errno.EILSEQ)
        return

    evidence = collect_evidence(path, readers=[FakeReader(name="a", values=("A",))])
    stored = evidence.model_dump_json()
    restored = EvidenceSet.model_validate_json(stored)

    assert_that(evidence.issues).is_empty()
    assert_that(stored).contains('"hex:')
    assert_that(restored).is_equal_to(evidence)
    snapshot = restored.snapshot
    if snapshot is None:
        pytest.fail("expected a snapshot")
    assert_that(snapshot.path).is_equal_to(path)
    assert_that({o.origin for o in restored.observations}).is_equal_to({path})


def test_collect_evidence_container_without_decision_signal(
    media_file: Path,
    spy: DetectSpy,
) -> None:
    """A container with no deciding signal to attribute it to is not recorded."""
    detection = detect(media_file).model_copy(
        update={
            "major_brand": None,
            "compatible_brands": (),
            "signals": (
                TypeSignal(source="extension", detail="suffix=.heic", media_type=None),
            ),
        },
    )

    evidence = collect_evidence(media_file, readers=(), detection=detection)

    assert_that(spy.calls).is_empty()
    assert_that([o.claim for o in evidence.observations]).is_equal_to(
        [Claim.MEDIA_TYPE],
    )


def test_collect_evidence_without_container_records_none(
    media_file: Path,
    spy: DetectSpy,
) -> None:
    """No container is recorded when detection established none."""
    detection = detect(media_file).model_copy(
        update={
            "container": None,
            "major_brand": None,
            "compatible_brands": (),
            "signals": (
                TypeSignal(
                    source="override",
                    detail="decision=override suffix=.heic",
                    media_type=MediaType.VIDEO,
                ),
            ),
        },
    )

    evidence = collect_evidence(media_file, readers=(), detection=detection)

    assert_that(spy.calls).is_empty()
    assert_that(
        [(o.claim, o.source, o.value) for o in evidence.observations],
    ).is_equal_to([(Claim.MEDIA_TYPE, SourceKind.OVERRIDE, "video")])


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
