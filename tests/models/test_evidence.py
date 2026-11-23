"""Tests for the evidence model (#394)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that
from pydantic import ValidationError

import winnow.models.evidence as evidence_module
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
    decode_path,
    encode_path,
    observation_id,
    snapshot_fingerprint,
)

GOLDEN_V1 = Path(__file__).resolve().parent / "data" / "evidence_v1.json"

KNOWN_CAPTURE_ID = (
    "sha256:a2a2ae76d4d78e409008b3045edc5e6200f2470862db14c38cb9ee7f0a0a2f28"
)
"""ID of the golden set's first observation; changes only with a bump."""

UNDECODABLE = Path(os.fsdecode(b"/photos/caf\xe9.jpg"))
"""A Latin-1 file name, valid on Linux, that is not UTF-8."""


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


def _time(**fields: Any) -> TimeValue:
    """Build a time value from loose fields, offset absent by default.

    Args:
        **fields: Field values; ``offset_minutes`` and ``offset_origin``
            default to ``None``.

    Returns:
        The validated value.
    """
    payload: dict[str, Any] = {"offset_minutes": None, "offset_origin": None}
    payload.update(fields)
    return TimeValue.model_validate(payload)


def _golden_set() -> EvidenceSet:
    """Build the evidence set pinned by ``data/evidence_v1.json``.

    Returns:
        A set with anchors, a source fraction, an unknown offset, a year, a
        ``hex:`` raw value, an undecodable path and every set field.
    """
    snapshot = FileSnapshot(
        path=Path("/photos/2019/IMG_0001.HEIC"),
        size=2_048_576,
        mtime_ns=1_562_231_410_123_456_789,
        device=16_777_229,
        inode=670_575_813,
        ctime_ns=1_562_231_411_000_000_001,
        birthtime_ns=1_562_231_409_000_000_000,
    )
    origin = snapshot.path
    observations = [
        _observation(
            origin=origin,
            raw="2019:07:04 10:10:10 | 123 | +09:00",
            locator="ExifIFD:DateTimeOriginal#0",
            value=_time(
                wall=datetime(2019, 7, 4, 10, 10, 10, 123000),
                offset_minutes=540,
                offset_origin="source",
                precision=Precision.SUBSECOND,
                fraction_digits=3,
            ),
        ),
        _observation(
            origin=origin,
            claim=Claim.MODIFIED_TIME,
            locator="IFD0:DateTime#0",
            raw="2021:01:01 09:00:00",
            value=_time(
                wall=datetime(2021, 1, 1, 9),
                precision=Precision.SECOND,
            ),
        ),
        _observation(
            origin=origin,
            claim=Claim.CONTAINER_CREATED_TIME,
            source=SourceKind.QUICKTIME,
            family="quicktime-mvhd",
            locator="QuickTime:mvhd:CreationTime#0",
            raw="3645158400",
            value=_time(
                wall=datetime(2019, 7, 4, 1, 10),
                offset_minutes=0,
                offset_origin="utc-by-spec",
                precision=Precision.MINUTE,
            ),
            reader="ffprobe",
            reader_version="8.0",
        ),
        _observation(
            origin=origin,
            claim=Claim.RELEASE_TIME,
            source=SourceKind.AUDIO_TAG,
            family="id3v2",
            locator="ID3:TORY#0",
            raw="1985",
            value=_time(wall=datetime(1985, 1, 1), precision=Precision.YEAR),
            reader="mutagen",
            reader_version="1.47.0",
        ),
        _observation(
            origin=origin,
            claim=Claim.CONTENT_ID,
            locator="ExifIFD:ImageUniqueID#0",
            raw="hex:00ff10",
            status=ParseStatus.MALFORMED,
        ),
        _observation(
            origin=origin,
            claim=Claim.MEDIA_TYPE,
            source=SourceKind.FTYP,
            family="ftyp",
            locator="detect:ftyp:decision#0",
            raw="decision=iso-bmff image brand",
            value="image",
            reader="winnow",
            reader_version="0.38.0",
        ),
    ]
    return EvidenceSet(
        snapshot=snapshot,
        observations=tuple(
            o.model_copy(update={"id": observation_id(o, snapshot=snapshot)})
            for o in observations
        ),
        issues=(
            ReadIssue(
                reader="ffprobe",
                status="backend-unavailable",
                message="ffprobe not found on PATH",
            ),
        ),
        dependencies=(snapshot.model_copy(update={"path": UNDECODABLE, "size": 512}),),
        association_fingerprint="sha256:" + "0" * 64,
    )


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
    value = _time(
        wall=datetime(2019, 7, 4, 10, 10, 10),
        offset_minutes=offset_minutes,
        offset_origin=origin,
        precision=Precision.SECOND,
    )

    instant = value.instant()

    assert_that(instant).is_equal_to(expected)
    if instant is not None:
        assert_that(instant.tzinfo).is_equal_to(UTC)


def test_time_value_instant_of_minute_is_its_start() -> None:
    """A minute value's instant is the first moment of that minute."""
    value = _time(
        wall=datetime(2019, 7, 4, 10, 10),
        offset_minutes=540,
        offset_origin="source",
        precision=Precision.MINUTE,
    )

    assert_that(value.instant()).is_equal_to(datetime(2019, 7, 4, 1, 10, tzinfo=UTC))


@pytest.mark.parametrize(
    ("fields", "rendered"),
    [
        pytest.param(
            {"wall": datetime(1985, 1, 1), "precision": Precision.YEAR},
            "1985",
            id="year",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 1), "precision": Precision.MONTH},
            "1985-07",
            id="month",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 4), "precision": Precision.DAY},
            "1985-07-04",
            id="day",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 4, 10, 10), "precision": Precision.MINUTE},
            "1985-07-04T10:10",
            id="minute",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 4, 10, 10, 10), "precision": Precision.SECOND},
            "1985-07-04T10:10:10",
            id="second",
        ),
        pytest.param(
            {
                "wall": datetime(1985, 7, 4, 10, 10, 10, 100000),
                "precision": Precision.SUBSECOND,
                "fraction_digits": 1,
            },
            "1985-07-04T10:10:10.1",
            id="one-fraction-digit",
        ),
        pytest.param(
            {
                "wall": datetime(1985, 7, 4, 10, 10, 10, 123000),
                "precision": Precision.SUBSECOND,
                "fraction_digits": 3,
            },
            "1985-07-04T10:10:10.123",
            id="three-fraction-digits",
        ),
        pytest.param(
            {
                "wall": datetime(1985, 7, 4, 10, 10, 10, 123456),
                "precision": Precision.SUBSECOND,
                "fraction_digits": 9,
            },
            "1985-07-04T10:10:10.123456",
            id="nine-digits-truncated-to-six",
        ),
        pytest.param(
            {"wall": datetime(1, 1, 1), "precision": Precision.YEAR},
            "0001",
            id="year-one",
        ),
        pytest.param(
            {"wall": datetime(999, 7, 4, 5, 6), "precision": Precision.MINUTE},
            "0999-07-04T05:06",
            id="year-999",
        ),
    ],
)
def test_time_value_renders_only_stated_components(
    fields: dict[str, Any],
    rendered: str,
) -> None:
    """A value renders the components and fraction digits the source gave."""
    value = _time(**fields)

    assert_that(value.render()).is_equal_to(rendered)
    assert_that(value.instant()).is_none()


def test_time_value_keeps_year_precision() -> None:
    """``1985`` stays a year: no invented day, time or instant."""
    value = _time(wall=datetime(1985, 1, 1), precision=Precision.YEAR)

    restored = TimeValue.model_validate_json(value.model_dump_json())

    assert_that(restored.precision).is_equal_to(Precision.YEAR)
    assert_that(restored.render()).is_equal_to("1985")
    assert_that(restored.instant()).is_none()


def test_time_value_keeps_fraction_digits() -> None:
    """``.123`` and ``.123000`` stay distinct after a round trip."""
    three = _time(
        wall=datetime(2019, 7, 4, 10, 10, 10, 123000),
        precision=Precision.SUBSECOND,
        fraction_digits=3,
    )
    six = three.model_copy(update={"fraction_digits": 6})

    restored = TimeValue.model_validate_json(three.model_dump_json())

    assert_that(restored).is_equal_to(three).is_not_equal_to(six)
    assert_that(restored.render()).ends_with(".123")


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
    value = _time(
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
            datetime(2019, 7, 4, 10, 10, 10, tzinfo=timezone(timedelta(hours=9))),
            id="plus-nine",
        ),
        pytest.param("2019-07-04T10:10:10+09:00", id="zoned-text"),
    ],
)
def test_time_value_rejects_aware_wall(wall: object) -> None:
    """``wall`` must be naive: the offset lives in ``offset_minutes``."""
    with pytest.raises(ValidationError):
        _time(wall=wall, precision=Precision.SECOND)


@pytest.mark.parametrize(
    "wall",
    [
        pytest.param("2019-07-04", id="date-text"),
        pytest.param("2019-07-04T10:10", id="no-seconds"),
        pytest.param("2019", id="year-text"),
        pytest.param(1_562_231_410, id="epoch-number"),
        pytest.param(datetime(2019, 7, 4).date(), id="date-object"),
    ],
)
def test_time_value_rejects_partial_wall_input(wall: object) -> None:
    """A partial or numeric wall is rejected, never read as midnight."""
    with pytest.raises(ValidationError, match="full"):
        _time(wall=wall, precision=Precision.SECOND)


def test_time_value_accepts_full_wall_text() -> None:
    """The stored text form of a wall is accepted."""
    value = _time(
        wall="2019-07-04T10:10:10.123", precision=Precision.SUBSECOND, fraction_digits=3
    )

    assert_that(value.wall).is_equal_to(datetime(2019, 7, 4, 10, 10, 10, 123000))


def test_time_value_rejects_folded_wall() -> None:
    """A naive wall has no fold; ``fold=1`` would be lost in JSON."""
    with pytest.raises(ValidationError, match="fold"):
        _time(wall=datetime(2019, 7, 4, 10, 10, 10, fold=1), precision=Precision.SECOND)


@pytest.mark.parametrize(
    "offset_minutes",
    [
        pytest.param(False, id="false"),
        pytest.param(True, id="true"),
        pytest.param(540.0, id="float"),
        pytest.param("540", id="text"),
    ],
)
def test_time_value_rejects_non_integer_offset(offset_minutes: object) -> None:
    """An offset is an integer, never a coerced boolean, float or text."""
    with pytest.raises(ValidationError):
        _time(
            wall=datetime(2019, 7, 4, 10, 10, 10),
            offset_minutes=offset_minutes,
            offset_origin="source",
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
        _time(
            wall=datetime(2019, 7, 4, 10, 10, 10),
            offset_minutes=offset_minutes,
            offset_origin=origin,
            precision=Precision.SECOND,
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
        _time(
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
    value = _time(
        wall=datetime(2019, 7, 4, 10, 10, 10),
        offset_minutes=offset_minutes,
        offset_origin="source",
        precision=Precision.SECOND,
    )

    assert_that(value.offset_minutes).is_equal_to(offset_minutes)


@pytest.mark.parametrize(
    ("wall", "offset_minutes"),
    [
        pytest.param(datetime(1, 1, 1), 840, id="before-year-one"),
        pytest.param(datetime(9999, 12, 31, 23, 59), -720, id="after-year-9999"),
    ],
)
def test_time_value_rejects_unrepresentable_instant(
    wall: datetime,
    offset_minutes: int,
) -> None:
    """A value whose UTC instant overflows is rejected, so instant never raises."""
    with pytest.raises(ValidationError, match="representable"):
        _time(
            wall=wall,
            offset_minutes=offset_minutes,
            offset_origin="source",
            precision=Precision.MINUTE,
        )


def test_time_value_rejects_offset_on_date_only() -> None:
    """A calendar date has no time of day, so an offset would invent one."""
    with pytest.raises(ValidationError, match="no time of day"):
        _time(
            wall=datetime(2019, 7, 4),
            offset_minutes=540,
            offset_origin="source",
            precision=Precision.DAY,
        )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param(
            {"precision": Precision.SUBSECOND},
            "needs fraction_digits",
            id="subsecond-without-digits",
        ),
        pytest.param(
            {"precision": Precision.SECOND, "fraction_digits": 3},
            "no fraction digits",
            id="second-with-digits",
        ),
        pytest.param(
            {"precision": Precision.SUBSECOND, "fraction_digits": 0},
            "greater than or equal",
            id="zero-digits",
        ),
    ],
)
def test_time_value_checks_fraction_digits(
    fields: dict[str, Any],
    message: str,
) -> None:
    """Fraction digits are given exactly for subsecond values."""
    with pytest.raises(ValidationError, match=message):
        _time(wall=datetime(2019, 7, 4, 10, 10, 10), **fields)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(
            {"wall": datetime(1985, 7, 1), "precision": Precision.YEAR},
            id="year-with-month",
        ),
        pytest.param(
            {"wall": datetime(1985, 1, 4), "precision": Precision.YEAR},
            id="year-with-day",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 4), "precision": Precision.MONTH},
            id="month-with-day",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 4, 9), "precision": Precision.DAY},
            id="day-with-hour",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 4, 9, 0, 5), "precision": Precision.MINUTE},
            id="minute-with-second",
        ),
        pytest.param(
            {"wall": datetime(1985, 7, 4, 9, 0, 5, 1), "precision": Precision.SECOND},
            id="second-with-microsecond",
        ),
        pytest.param(
            {
                "wall": datetime(1985, 7, 4, 9, 0, 5, 123400),
                "precision": Precision.SUBSECOND,
                "fraction_digits": 3,
            },
            id="fraction-past-its-digits",
        ),
    ],
)
def test_time_value_rejects_components_below_precision(fields: dict[str, Any]) -> None:
    """Components the source does not state must hold their anchors."""
    with pytest.raises(ValidationError, match="anchors"):
        _time(**fields)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(Path("/photos/IMG_0001.JPG"), id="ascii"),
        pytest.param(Path("/photos/café/ü.jpg"), id="unicode"),
        pytest.param(UNDECODABLE, id="undecodable-byte"),
        pytest.param(Path("hex:6162"), id="text-starting-with-hex"),
        pytest.param(Path("relative/x.jpg"), id="relative"),
    ],
)
def test_path_encoding_round_trips(path: Path) -> None:
    """Every path a filesystem allows encodes to valid text and back."""
    encoded = encode_path(path)

    assert_that(encoded.encode("utf-8")).is_not_empty()
    assert_that(decode_path(encoded)).is_equal_to(path)


def test_path_encoding_uses_hex_for_undecodable_bytes() -> None:
    """An undecodable name is stored as its bytes in hex."""
    assert_that(encode_path(UNDECODABLE)).is_equal_to(
        "hex:" + b"/photos/caf\xe9.jpg".hex(),
    )
    assert_that(encode_path(Path("/photos/café.jpg"))).is_equal_to(
        "/photos/café.jpg",
    )


def test_evidence_set_stores_undecodable_path(snapshot: FileSnapshot) -> None:
    """A set for a non-UTF-8 file name serialises, hashes and round-trips."""
    odd = snapshot.model_copy(update={"path": UNDECODABLE})
    observation = _observation(origin=UNDECODABLE)
    evidence = EvidenceSet(
        snapshot=odd,
        observations=(
            observation.model_copy(
                update={"id": observation_id(observation, snapshot=odd)},
            ),
        ),
        issues=(ReadIssue(reader="x", status="failed", message=str(UNDECODABLE)),),
    )

    text = evidence.model_dump_json()
    restored = EvidenceSet.model_validate_json(text)

    assert_that(restored.snapshot).is_equal_to(odd)
    assert_that(restored.observations).is_equal_to(evidence.observations)
    assert_that(restored.issues[0].message).is_equal_to("/photos/caf\\udce9.jpg")
    assert_that(
        EvidenceSet.model_validate(evidence.model_dump(mode="json"))
    ).is_equal_to(
        evidence,
    )


def test_observation_rejects_undecodable_text() -> None:
    """Raw text must be valid Unicode; bytes are written as ``hex:``."""
    with pytest.raises(ValidationError, match="hex:"):
        _observation(raw=os.fsdecode(b"\xff"))


def test_evidence_set_json_round_trip(
    snapshot: FileSnapshot,
    capture_time: TimeValue,
) -> None:
    """The stored JSON form restores an equal set, every source included."""
    observations = tuple(
        _observation(source=source, locator=f"test:{source}#0", value=capture_time)
        for source in SourceKind
    ) + (
        _observation(
            claim=Claim.RELEASE_TIME,
            source=SourceKind.AUDIO_TAG,
            locator="ID3:TORY#0",
            raw="1985",
            value=_time(wall=datetime(1985, 1, 1), precision=Precision.YEAR),
        ),
        _observation(
            claim=Claim.CONTENT_ID,
            locator="ExifIFD:ImageUniqueID#0",
            raw="hex:00ff10",
            status=ParseStatus.MALFORMED,
        ),
    )
    evidence = EvidenceSet(
        snapshot=snapshot,
        observations=observations,
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
    assert_that(restored.observations[-2].value).is_instance_of(TimeValue)
    assert_that(restored.observations[-1].raw).is_equal_to("hex:00ff10")


def test_evidence_set_matches_stored_v1_bytes() -> None:
    """The stored version-1 form is pinned byte for byte."""
    evidence = _golden_set()
    committed = GOLDEN_V1.read_text(encoding="utf-8")
    stored = json.dumps(
        json.loads(committed),
        separators=(",", ":"),
        ensure_ascii=False,
    )

    assert_that(evidence.model_dump_json()).is_equal_to(stored)
    assert_that(EvidenceSet.model_validate_json(committed)).is_equal_to(evidence)


def test_observation_id_is_pinned() -> None:
    """One known ID pins the identity rule and its canonical form."""
    assert_that(_golden_set().observations[0].id).is_equal_to(KNOWN_CAPTURE_ID)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool"),
        pytest.param(2, id="newer"),
        pytest.param(0, id="zero"),
        pytest.param("1", id="text"),
    ],
)
def test_evidence_set_rejects_unsupported_schema_version(
    snapshot: FileSnapshot,
    value: object,
) -> None:
    """Only version 1, as an integer, is read as version 1."""
    with pytest.raises(ValidationError):
        EvidenceSet.model_validate(
            {
                "schema_version": value,
                "snapshot": snapshot.model_dump(mode="json"),
                "observations": [],
                "issues": [],
            },
        )


def test_evidence_set_without_snapshot_needs_unreadable_issue() -> None:
    """A set with no snapshot must say the file was unreadable."""
    with pytest.raises(ValidationError, match="unreadable"):
        EvidenceSet(snapshot=None, observations=(), issues=())
    with pytest.raises(ValidationError, match="snapshot"):
        EvidenceSet(
            snapshot=None,
            observations=(_observation(),),
            issues=(ReadIssue(reader="winnow", status="unreadable", message="x"),),
        )


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
        locator="IFD0:DateTime#0",
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
        pytest.param({"claim": Claim.MODIFIED_TIME}, id="claim"),
        pytest.param({"source": SourceKind.XMP}, id="source"),
        pytest.param({"family": "xmp"}, id="family"),
        pytest.param({"locator": "ExifIFD:DateTimeOriginal#1"}, id="locator"),
        pytest.param({"raw": "2019:07:04 10:10:11"}, id="raw"),
        pytest.param({"raw": None}, id="raw-absent"),
        pytest.param({"status": ParseStatus.MALFORMED}, id="status"),
        pytest.param({"origin": Path("/photos/IMG_0002.JPG")}, id="origin"),
        pytest.param({"reader": "exifread"}, id="reader"),
        pytest.param({"reader_version": "12.0.1"}, id="reader-version"),
        pytest.param({"value": "x"}, id="value-text"),
        pytest.param(
            {
                "value": TimeValue(
                    wall=datetime(2019, 7, 4, 10, 10, 10),
                    offset_minutes=None,
                    offset_origin=None,
                    precision=Precision.SECOND,
                ),
            },
            id="value-time",
        ),
    ],
)
def test_observation_id_covers_observation_fields(
    snapshot: FileSnapshot,
    change: dict[str, Any],
) -> None:
    """Each observation field in the identity changes the ID."""
    base = _observation()

    changed = base.model_copy(update=change)

    assert_that(observation_id(changed, snapshot=snapshot)).is_not_equal_to(
        observation_id(base, snapshot=snapshot),
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"path": Path("/photos/IMG_0002.JPG")}, id="path"),
        pytest.param({"size": 1235}, id="size"),
        pytest.param({"mtime_ns": 1_562_231_410_000_000_001}, id="mtime_ns"),
        pytest.param({"device": 17}, id="device"),
        pytest.param({"inode": 43}, id="inode"),
        pytest.param({"ctime_ns": 1_562_231_411_000_000_001}, id="ctime_ns"),
        pytest.param({"birthtime_ns": 1_562_231_400_000_000_000}, id="birthtime_ns"),
    ],
)
def test_observation_id_covers_snapshot_fields(
    snapshot: FileSnapshot,
    change: dict[str, Any],
) -> None:
    """Each snapshot field changes the fingerprint and so the ID."""
    observation = _observation()
    changed = snapshot.model_copy(update=change)

    assert_that(snapshot_fingerprint(changed)).is_not_equal_to(
        snapshot_fingerprint(snapshot),
    )
    assert_that(observation_id(observation, snapshot=changed)).is_not_equal_to(
        observation_id(observation, snapshot=snapshot),
    )


def test_observation_id_covers_schema_version(
    snapshot: FileSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema version bump changes every ID."""
    observation = _observation()
    current = observation_id(observation, snapshot=snapshot)

    monkeypatch.setattr(evidence_module, "EVIDENCE_SCHEMA_VERSION", 2)

    assert_that(observation_id(observation, snapshot=snapshot)).is_not_equal_to(
        current,
    )


def test_observation_id_without_snapshot(snapshot: FileSnapshot) -> None:
    """A missing snapshot is part of the identity too."""
    observation = _observation()

    assert_that(snapshot_fingerprint(None)).is_none()
    assert_that(observation_id(observation, snapshot=None)).starts_with(
        "sha256:",
    ).is_not_equal_to(observation_id(observation, snapshot=snapshot))


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


@pytest.mark.parametrize(
    "observation",
    [pytest.param(-1, id="negative"), pytest.param(True, id="bool")],
)
def test_rejection_rejects_bad_index(observation: object) -> None:
    """Indices into an evidence set are non-negative integers."""
    with pytest.raises(ValidationError):
        Rejection.model_validate({"observation": observation, "reason": "conflict"})
