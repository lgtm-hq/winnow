"""Tests for the evidence model (#394)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that
from pydantic import ValidationError

from winnow.models import EvidenceSet as PublicEvidenceSet
from winnow.models.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    Certainty,
    Claim,
    Decision,
    EvidenceSet,
    FileSnapshot,
    Observation,
    ParseStatus,
    Precision,
    ReadIssue,
    Rejection,
    SourceKind,
    TimeValue,
    observation_id,
    snapshot_fingerprint,
)


@pytest.fixture
def snapshot() -> FileSnapshot:
    """Return a snapshot of a made-up file.

    Returns:
        The snapshot.
    """
    return FileSnapshot(
        path=Path("/photos/IMG_0001.JPG"),
        size=1234,
        mtime_ns=1_562_231_410_000_000_000,
        device=16,
        inode=42,
        ctime_ns=1_562_231_411_000_000_000,
        birthtime_ns=None,
    )


@pytest.fixture
def capture_time() -> TimeValue:
    """Return a capture time with an offset from the source.

    Returns:
        ``2019-07-04T10:10:10+09:00``.
    """
    return TimeValue(
        wall=datetime(2019, 7, 4, 10, 10, 10),
        offset_minutes=540,
        offset_origin="source",
        precision=Precision.SECOND,
    )


def _observation(**overrides: Any) -> Observation:
    """Build an observation, overriding any field.

    Args:
        **overrides: Field values to replace.

    Returns:
        The observation.
    """
    fields: dict[str, Any] = {
        "id": "",
        "claim": Claim.CAPTURE_TIME,
        "source": SourceKind.EXIF,
        "locator": "ExifIFD:DateTimeOriginal",
        "origin": Path("/photos/IMG_0001.JPG"),
        "raw": "2019:07:04 10:10:10",
        "value": None,
        "status": ParseStatus.OK,
        "family": "exif",
        "reader": "pillow",
        "reader_version": "12.0.0",
    }
    fields.update(overrides)
    return Observation(**fields)


def test_public_names_import() -> None:
    """The evidence names are exported from the models package too."""
    assert_that(PublicEvidenceSet).is_same_as(EvidenceSet)
    assert_that(EVIDENCE_SCHEMA_VERSION).is_equal_to(1)


@pytest.mark.parametrize(
    ("offset_minutes", "origin", "expected"),
    [
        pytest.param(
            540,
            "source",
            datetime(2019, 7, 4, 1, 10, 10, tzinfo=UTC),
            id="plus-nine-hours",
        ),
        pytest.param(
            -270,
            "source",
            datetime(2019, 7, 4, 14, 40, 10, tzinfo=UTC),
            id="minus-four-thirty",
        ),
        pytest.param(
            0,
            "utc-by-spec",
            datetime(2019, 7, 4, 10, 10, 10, tzinfo=UTC),
            id="utc-by-spec",
        ),
        pytest.param(None, None, None, id="no-offset"),
    ],
)
def test_time_value_instant_requires_offset(
    offset_minutes: int | None,
    origin: str | None,
    expected: datetime | None,
) -> None:
    """``instant()`` is an aware UTC moment only when the offset is known."""
    value = TimeValue.model_validate(
        {
            "wall": datetime(2019, 7, 4, 10, 10, 10),
            "offset_minutes": offset_minutes,
            "offset_origin": origin,
            "precision": Precision.SECOND,
        },
    )

    instant = value.instant()

    assert_that(instant).is_equal_to(expected)
    if instant is not None:
        assert_that(instant.tzinfo).is_equal_to(UTC)


@pytest.mark.parametrize(
    ("wall", "precision", "rendered"),
    [
        pytest.param(datetime(1985, 1, 1), Precision.YEAR, "1985", id="year"),
        pytest.param(datetime(1985, 7, 1), Precision.MONTH, "1985-07", id="month"),
        pytest.param(datetime(1985, 7, 4), Precision.DAY, "1985-07-04", id="day"),
        pytest.param(
            datetime(1985, 7, 4, 10, 10),
            Precision.MINUTE,
            "1985-07-04T10:10",
            id="minute",
        ),
        pytest.param(
            datetime(1985, 7, 4, 10, 10, 10),
            Precision.SECOND,
            "1985-07-04T10:10:10",
            id="second",
        ),
        pytest.param(
            datetime(1985, 7, 4, 10, 10, 10, 123000),
            Precision.SUBSECOND,
            "1985-07-04T10:10:10.123000",
            id="subsecond",
        ),
    ],
)
def test_time_value_renders_only_stated_components(
    wall: datetime,
    precision: Precision,
    rendered: str,
) -> None:
    """A value renders the components its precision states and no others."""
    value = TimeValue(
        wall=wall,
        offset_minutes=None,
        offset_origin=None,
        precision=precision,
    )

    assert_that(value.render()).is_equal_to(rendered)
    assert_that(value.instant()).is_none()


def test_time_value_keeps_year_precision() -> None:
    """``1985`` stays a year: no invented day, time or instant."""
    value = TimeValue(
        wall=datetime(1985, 1, 1),
        offset_minutes=None,
        offset_origin=None,
        precision=Precision.YEAR,
    )

    restored = TimeValue.model_validate_json(value.model_dump_json())

    assert_that(restored.precision).is_equal_to(Precision.YEAR)
    assert_that(restored.render()).is_equal_to("1985")
    assert_that(restored.instant()).is_none()


@pytest.mark.parametrize(
    ("offset_minutes", "rendered"),
    [
        pytest.param(540, "2019-07-04T10:10:10+09:00", id="east"),
        pytest.param(-270, "2019-07-04T10:10:10-04:30", id="west"),
        pytest.param(0, "2019-07-04T10:10:10+00:00", id="zero"),
    ],
)
def test_time_value_renders_offset(offset_minutes: int, rendered: str) -> None:
    """A known offset is appended to the rendered value."""
    value = TimeValue(
        wall=datetime(2019, 7, 4, 10, 10, 10),
        offset_minutes=offset_minutes,
        offset_origin="source",
        precision=Precision.SECOND,
    )

    assert_that(value.render()).is_equal_to(rendered)


@pytest.mark.parametrize(
    "wall",
    [
        pytest.param(datetime(2019, 7, 4, 10, 10, 10, tzinfo=UTC), id="utc"),
        pytest.param(
            datetime(
                2019,
                7,
                4,
                10,
                10,
                10,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            id="plus-nine",
        ),
    ],
)
def test_time_value_rejects_aware_wall(wall: datetime) -> None:
    """``wall`` must be naive: the offset lives in ``offset_minutes``."""
    with pytest.raises(ValidationError, match="naive"):
        TimeValue(
            wall=wall,
            offset_minutes=None,
            offset_origin=None,
            precision=Precision.SECOND,
        )


@pytest.mark.parametrize(
    ("offset_minutes", "origin", "message"),
    [
        pytest.param(None, "source", "requires offset_minutes", id="origin-only"),
        pytest.param(540, None, "requires offset_origin", id="offset-only"),
        pytest.param(540, "utc-by-spec", "must be 0", id="utc-by-spec-not-zero"),
    ],
)
def test_time_value_rejects_inconsistent_offset_origin(
    offset_minutes: int | None,
    origin: str | None,
    message: str,
) -> None:
    """An offset and its origin are both present or both absent."""
    with pytest.raises(ValidationError, match=message):
        TimeValue.model_validate(
            {
                "wall": datetime(2019, 7, 4, 10, 10, 10),
                "offset_minutes": offset_minutes,
                "offset_origin": origin,
                "precision": Precision.SECOND,
            },
        )


@pytest.mark.parametrize(
    "offset_minutes",
    [
        pytest.param(-721, id="below-minus-twelve"),
        pytest.param(841, id="above-plus-fourteen"),
    ],
)
def test_time_value_rejects_offset_out_of_range(offset_minutes: int) -> None:
    """Offsets outside ``-12:00..+14:00`` are invalid."""
    with pytest.raises(ValidationError, match="within"):
        TimeValue(
            wall=datetime(2019, 7, 4, 10, 10, 10),
            offset_minutes=offset_minutes,
            offset_origin="source",
            precision=Precision.SECOND,
        )


@pytest.mark.parametrize(
    "offset_minutes",
    [pytest.param(-720, id="minus-twelve"), pytest.param(840, id="plus-fourteen")],
)
def test_time_value_accepts_offset_range_ends(offset_minutes: int) -> None:
    """``-12:00`` and ``+14:00`` are valid offsets."""
    value = TimeValue(
        wall=datetime(2019, 7, 4, 10, 10, 10),
        offset_minutes=offset_minutes,
        offset_origin="source",
        precision=Precision.SECOND,
    )

    assert_that(value.offset_minutes).is_equal_to(offset_minutes)


def test_time_value_rejects_offset_on_date_only() -> None:
    """A calendar date has no time of day, so an offset would invent one."""
    with pytest.raises(ValidationError, match="no time of day"):
        TimeValue(
            wall=datetime(2019, 7, 4),
            offset_minutes=540,
            offset_origin="source",
            precision=Precision.DAY,
        )


@pytest.mark.parametrize(
    ("wall", "precision"),
    [
        pytest.param(datetime(1985, 7, 1), Precision.YEAR, id="year-with-month"),
        pytest.param(datetime(1985, 1, 4), Precision.YEAR, id="year-with-day"),
        pytest.param(datetime(1985, 7, 4), Precision.MONTH, id="month-with-day"),
        pytest.param(datetime(1985, 7, 4, 9), Precision.DAY, id="day-with-hour"),
        pytest.param(
            datetime(1985, 7, 4, 9, 0, 5),
            Precision.MINUTE,
            id="minute-with-second",
        ),
        pytest.param(
            datetime(1985, 7, 4, 9, 0, 5, 1),
            Precision.SECOND,
            id="second-with-microsecond",
        ),
    ],
)
def test_time_value_rejects_components_below_precision(
    wall: datetime,
    precision: Precision,
) -> None:
    """Components the source does not state must hold their anchors."""
    with pytest.raises(ValidationError, match="anchors"):
        TimeValue(
            wall=wall,
            offset_minutes=None,
            offset_origin=None,
            precision=precision,
        )


def test_evidence_set_json_round_trip(
    snapshot: FileSnapshot,
    capture_time: TimeValue,
) -> None:
    """The stored JSON form restores an equal set, every source included."""
    year = TimeValue(
        wall=datetime(1985, 1, 1),
        offset_minutes=None,
        offset_origin=None,
        precision=Precision.YEAR,
    )
    observations = tuple(
        _observation(
            source=source,
            locator=f"test:{source}",
            value=capture_time,
        )
        for source in SourceKind
    ) + (
        _observation(
            claim=Claim.RELEASE_TIME,
            source=SourceKind.AUDIO_TAG,
            locator="ID3:TORY",
            raw="1985",
            value=year,
        ),
        _observation(
            claim=Claim.CONTENT_ID,
            source=SourceKind.EXIF,
            locator="ExifIFD:ImageUniqueID",
            raw="hex:00ff10",
            value=None,
            status=ParseStatus.MALFORMED,
        ),
        _observation(
            claim=Claim.MEDIA_TYPE,
            source=SourceKind.FTYP,
            locator="detect:signals[0]",
            raw="decision=iso-bmff image brand",
            value="image",
        ),
    )
    evidence = EvidenceSet(
        snapshot=snapshot,
        observations=tuple(
            observation.model_copy(
                update={"id": observation_id(observation, snapshot=snapshot)},
            )
            for observation in observations
        ),
        issues=(
            ReadIssue(reader="ffprobe", status="backend-unavailable", message="x"),
        ),
        dependencies=(snapshot.model_copy(update={"path": Path("/photos/a.xmp")}),),
        association_fingerprint="sha256:abc",
    )

    restored = EvidenceSet.model_validate_json(evidence.model_dump_json())

    assert_that(restored).is_equal_to(evidence)
    assert_that({o.source for o in restored.observations}).is_equal_to(
        set(SourceKind),
    )
    assert_that(restored.observations[-3].value).is_equal_to(year)
    assert_that(restored.observations[-1].value).is_instance_of(str)


def test_evidence_set_rejects_unknown_field(snapshot: FileSnapshot) -> None:
    """A stored set with a field this schema lacks is rejected, not dropped."""
    payload = EvidenceSet(
        snapshot=snapshot,
        observations=(),
        issues=(),
    ).model_dump(mode="json")
    payload["surprise"] = 1

    with pytest.raises(ValidationError):
        EvidenceSet.model_validate(payload)


def test_evidence_models_are_frozen(capture_time: TimeValue) -> None:
    """Stored evidence cannot be changed in place."""
    with pytest.raises(ValidationError):
        capture_time.offset_minutes = 0


def test_observation_id_ignores_collection_order(
    snapshot: FileSnapshot,
    capture_time: TimeValue,
) -> None:
    """An ID depends on the observation and snapshot, not on neighbours."""
    first = _observation(value=capture_time)
    second = _observation(
        locator="IFD0:DateTime",
        claim=Claim.MODIFIED_TIME,
        raw="2021:01:01 09:00:00",
    )

    forward = [observation_id(o, snapshot=snapshot) for o in (first, second)]
    backward = [observation_id(o, snapshot=snapshot) for o in (second, first)]

    assert_that(forward).is_equal_to(list(reversed(backward)))
    assert_that(
        observation_id(first.model_copy(update={"id": "other"}), snapshot=snapshot),
    ).is_equal_to(forward[0])
    assert_that(forward[0]).starts_with("sha256:").is_length(7 + 64)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"locator": "ExifIFD:DateTimeOriginal#2"}, id="locator"),
        pytest.param({"raw": "2019:07:04 10:10:11"}, id="raw"),
        pytest.param({"status": ParseStatus.MALFORMED}, id="status"),
        pytest.param({"origin": Path("/photos/IMG_0001.xmp")}, id="origin"),
        pytest.param({"reader": "exifread"}, id="reader"),
        pytest.param({"reader_version": "12.0.1"}, id="reader-version"),
        pytest.param({"value": "x"}, id="value"),
    ],
)
def test_observation_id_covers_identity_fields(
    snapshot: FileSnapshot,
    change: dict[str, Any],
) -> None:
    """Each identity field changes the ID."""
    base = _observation()

    changed = base.model_copy(update=change)

    assert_that(observation_id(changed, snapshot=snapshot)).is_not_equal_to(
        observation_id(base, snapshot=snapshot),
    )


def test_observation_id_depends_on_snapshot(snapshot: FileSnapshot) -> None:
    """A rewritten file gives its observations new IDs."""
    observation = _observation()
    rewritten = snapshot.model_copy(update={"ctime_ns": snapshot.ctime_ns + 1})

    assert_that(observation_id(observation, snapshot=rewritten)).is_not_equal_to(
        observation_id(observation, snapshot=snapshot),
    )
    assert_that(snapshot_fingerprint(None)).is_none()
    assert_that(observation_id(observation, snapshot=None)).starts_with("sha256:")


def test_decision_keeps_rule_and_indices(capture_time: TimeValue) -> None:
    """A decision records its rule, chosen and rejected observations."""
    decision = Decision(
        claim=Claim.CAPTURE_TIME,
        value=capture_time,
        certainty=Certainty.CONFIRMED,
        rule_id="capture-time",
        rule_version=1,
        chosen=(0,),
        rejected=(Rejection(observation=1, reason="edit-time"),),
        conflicts=(),
    )

    restored = Decision.model_validate_json(decision.model_dump_json())

    assert_that(restored).is_equal_to(decision)
    assert_that(restored.warnings).is_empty()
    assert_that(restored.incomparable).is_empty()


def test_rejection_rejects_negative_index() -> None:
    """Indices into an evidence set are never negative."""
    with pytest.raises(ValidationError):
        Rejection(observation=-1, reason="conflict")
