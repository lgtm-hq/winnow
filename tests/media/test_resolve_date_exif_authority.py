"""Tests for the second review round of the date resolver (#396, PR #486).

The camera's native EXIF original is the authority for the written local
date and time; an embedded XMP copy of the original never replaces it.
Every case sits on a month boundary: 2019-08-01 00:10:10 at +09:00 is
2019-07-31 15:10:10 UTC, and each is checked with the observations in both
orders.
"""

from __future__ import annotations

import pytest
from assertpy import assert_that

from tests.media.date_evidence_support import (
    dto,
    evidence,
    gps,
    hint,
    iptc,
    obs,
    policy,
    reasons,
    software,
    xmp_created,
)
from winnow.media.resolve_date import resolve_capture_date
from winnow.models.evidence import (
    Certainty,
    Claim,
    Decision,
    Observation,
    ReadIssue,
    SourceKind,
)

ORDERS: list[object] = [
    pytest.param(False, id="exif-first"),
    pytest.param(True, id="xmp-first"),
]


def _xmp(text: str, offset: int | None = None) -> Observation:
    """Build an embedded XMP ``exif:DateTimeOriginal`` copy.

    Args:
        text: The value text.
        offset: Its offset from the file, or ``None``.

    Returns:
        The observation.
    """
    return dto(text, offset, source=SourceKind.XMP)


def _resolve(observations: list[Observation], *, reverse: bool) -> Decision:
    """Resolve the observations, optionally in reverse order.

    Args:
        observations: Native EXIF first, then the XMP copy, then extras.
        reverse: Whether to resolve them in reverse order.

    Returns:
        The decision.
    """
    ordered = list(reversed(observations)) if reverse else observations
    return resolve_capture_date(evidence(*ordered), policy=policy())


def _index(
    observation: Observation, observations: list[Observation], *, reverse: bool
) -> int:
    """Return where an observation lands in the resolved order.

    Args:
        observation: The observation.
        observations: The observations as listed.
        reverse: Whether they were reversed.

    Returns:
        Its index in the evidence set.
    """
    index = observations.index(observation)
    return len(observations) - 1 - index if reverse else index


@pytest.mark.parametrize("reverse", ORDERS)
@pytest.mark.parametrize(
    "copy_text",
    [
        pytest.param("2019-07-31T15:10:10", id="coarser-copy"),
        pytest.param("2019-07-31T15:10", id="minute-copy"),
    ],
)
def test_case_1_zoned_exif_keeps_its_value_over_a_coarser_utc_copy(
    copy_text: str,
    reverse: bool,
) -> None:
    """Case 1: the finer zoned EXIF stays; its coarser UTC copy supports it.

    Args:
        copy_text: The XMP copy, in UTC, coarser than the EXIF value.
        reverse: Whether the observations are reversed.
    """
    exif = dto("2019-08-01T00:10:10.555", 540)
    observations = [exif, _xmp(copy_text, 0)]

    decision = _resolve(observations, reverse=reverse)

    assert_that(decision.rule_id).is_equal_to("exif-original-offset")
    assert_that(decision.value).is_equal_to(exif.value)
    assert_that(decision.chosen).is_length(2)
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)


@pytest.mark.parametrize("reverse", ORDERS)
def test_case_2_zoned_exif_keeps_its_value_over_a_finer_utc_copy(reverse: bool) -> None:
    """Case 2: a finer UTC copy of the same instant never becomes the value.

    Args:
        reverse: Whether the observations are reversed.
    """
    exif = dto("2019-08-01T00:10:10", 540)
    observations = [exif, _xmp("2019-07-31T15:10:10.555", 0)]

    decision = _resolve(observations, reverse=reverse)

    assert_that(decision.rule_id).is_equal_to("exif-original-offset")
    assert_that(decision.value).is_equal_to(exif.value)
    assert_that(decision.chosen).is_length(2)


@pytest.mark.parametrize("reverse", ORDERS)
@pytest.mark.parametrize("with_gps", [False, True], ids=["no-gps", "utc-gps"])
def test_case_3_naive_exif_is_not_outranked_by_a_zoned_copy(
    with_gps: bool,
    reverse: bool,
) -> None:
    """Case 3: naive EXIF August 1 stays; a UTC XMP July 31 is a hint.

    Args:
        with_gps: Whether a UTC GPS stamp of the same instant is present.
        reverse: Whether the observations are reversed.
    """
    exif = dto("2019-08-01T00:10:10")
    copy = _xmp("2019-07-31T15:10:10", 0)
    observations = [exif, copy]
    if with_gps:
        observations.append(gps("2019-07-31T15:10:10"))

    decision = _resolve(observations, reverse=reverse)

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")
    assert_that(decision.value).is_equal_to(exif.value)
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.conflicts).is_empty()
    assert_that(decision.incomparable).contains(
        _index(copy, observations, reverse=reverse),
    )


@pytest.mark.parametrize("reverse", ORDERS)
def test_case_4_zoned_originals_for_different_instants_abstain(reverse: bool) -> None:
    """Case 4: the same written time at +09:00 and +08:00 is a contradiction.

    Args:
        reverse: Whether the observations are reversed.
    """
    observations = [
        dto("2019-08-01T00:10:10", 540),
        _xmp("2019-08-01T00:10:10", 480),
    ]

    decision = _resolve(observations, reverse=reverse)

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")
    assert_that(decision.value).is_none()
    assert_that(decision.conflicts).is_length(2)


@pytest.mark.parametrize("reverse", ORDERS)
@pytest.mark.parametrize(
    ("copy_text", "rule_id", "reason"),
    [
        pytest.param(
            "2019-08-01T00:10:10",
            "exif-original-wallclock",
            None,
            id="same-digits",
        ),
        pytest.param(
            "2019-07-31T15:10:10",
            "conflicting-capture",
            None,
            id="different-full-reading",
        ),
        pytest.param(
            "2019-07-31",
            "exif-original-wallclock",
            "copy-differs",
            id="different-day-only",
        ),
        pytest.param(
            "2019-07-31T15:10:10.5",
            "exif-original-wallclock",
            "copy-differs",
            id="different-precision",
        ),
    ],
)
def test_case_5_naive_exif_and_naive_copy(
    copy_text: str,
    rule_id: str,
    reason: str | None,
    reverse: bool,
) -> None:
    """Case 5: naive copies agree by digits; only like readings contradict.

    Args:
        copy_text: The naive XMP copy.
        rule_id: The expected rule.
        reason: The copy's rejection code, when it is a hint.
        reverse: Whether the observations are reversed.
    """
    exif = dto("2019-08-01T00:10:10")
    copy = _xmp(copy_text)
    observations = [exif, copy]

    decision = _resolve(observations, reverse=reverse)

    assert_that(decision.rule_id).is_equal_to(rule_id)
    if rule_id != "conflicting-capture":
        assert_that(decision.value).is_equal_to(exif.value)
        assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    copy_index = _index(copy, observations, reverse=reverse)
    assert_that(reasons(decision).get(copy_index)).is_equal_to(reason)


@pytest.mark.parametrize("reverse", ORDERS)
def test_zoned_exif_and_naive_copy_with_other_digits_is_a_hint(reverse: bool) -> None:
    """A naive copy written in UTC digits gives no zone mapping: incomparable.

    Args:
        reverse: Whether the observations are reversed.
    """
    exif = dto("2019-08-01T00:10:10", 540)
    copy = _xmp("2019-07-31T15:10:10")
    observations = [exif, copy]

    decision = _resolve(observations, reverse=reverse)

    assert_that(decision.value).is_equal_to(exif.value)
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.conflicts).is_empty()
    assert_that(decision.incomparable).is_equal_to(
        (_index(copy, observations, reverse=reverse),),
    )


@pytest.mark.parametrize(
    ("observation", "rule_id"),
    [
        pytest.param(
            _xmp("2019-07-31T15:10:10", 0),
            "exif-original-offset",
            id="zoned-xmp",
        ),
        pytest.param(
            _xmp("2019-08-01T00:10:10"),
            "exif-original-wallclock",
            id="naive-xmp",
        ),
    ],
)
def test_case_6_xmp_original_dates_a_file_without_native_exif(
    observation: Observation,
    rule_id: str,
) -> None:
    """Case 6: with no native EXIF original the XMP original dates the file.

    Args:
        observation: The XMP original.
        rule_id: The rule that reads it.
    """
    decision = resolve_capture_date(evidence(observation), policy=policy())

    assert_that(decision.rule_id).is_equal_to(rule_id)
    assert_that(decision.value).is_equal_to(observation.value)


@pytest.mark.parametrize(
    ("day", "certainty"),
    [
        pytest.param("2019-08-01", Certainty.CONFIRMED, id="same-written-day"),
        pytest.param("2019-07-31", Certainty.PROBABLE, id="other-day"),
    ],
)
def test_day_only_date_created_on_the_written_day_does_not_block(
    day: str,
    certainty: Certainty,
) -> None:
    """A day-only ``DateCreated`` on the camera's written day lets GPS confirm.

    Args:
        day: The day-only XMP ``DateCreated``.
        certainty: The expected certainty.
    """
    observations = [
        dto("2019-08-01T00:10:10", 540),
        gps("2019-07-31T15:10:10"),
        xmp_created(day),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(certainty)


def test_full_incomparable_reading_still_blocks_confirmed() -> None:
    """A full IPTC time without an offset still keeps GPS from confirming."""
    observations = [
        dto("2019-08-01T00:10:10", 540),
        gps("2019-07-31T15:10:10"),
        iptc("2019-08-01T00:10:10"),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)


@pytest.mark.parametrize(
    ("text", "blocks"),
    [
        pytest.param("Adobe Photoshop Lightroom 6.0", True, id="lightroom"),
        pytest.param("GIMP 2.10", True, id="gimp-version"),
        pytest.param("GIMP", True, id="gimp-bare"),
        pytest.param("darktable", True, id="darktable-bare"),
        pytest.param("Adobe PhotoshopCompatible Camera 1", False, id="glued-word"),
        pytest.param("MyGIMPCam 1.0", False, id="mid-word"),
        pytest.param("Gimpel Firmware 2", False, id="longer-word"),
    ],
)
def test_editor_names_match_on_a_token_boundary(text: str, blocks: bool) -> None:
    """Editor names match whole leading tokens only.

    Args:
        text: The ``Software`` value.
        blocks: Whether it names a known editor.
    """
    observations = [
        dto("2019-08-01T00:10:10", 540),
        gps("2019-07-31T15:10:10"),
        software(text),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that("editing-software" in decision.warnings).is_equal_to(blocks)
    expected = Certainty.PROBABLE if blocks else Certainty.CONFIRMED
    assert_that(decision.certainty).is_equal_to(expected)


def test_contradicted_reset_keeps_its_warning() -> None:
    """An undated reset-like conflict still says the clock looked reset."""
    observations = [dto("2000-01-01T00:00:00"), xmp_created("2024-07-09T10:00:00")]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")
    assert_that(decision.warnings).contains("suspicious-clock-default")


def test_offset_date_created_beats_a_finer_naive_copy() -> None:
    """A zoned IPTC time is kept over a finer naive XMP copy of its digits."""
    zoned = iptc("2019-08-01T00:10:10", 540)
    observations = [xmp_created("2019-08-01T00:10:10.12"), zoned]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.value).is_equal_to(zoned.value)


def test_finer_date_created_copy_on_another_day_abstains() -> None:
    """A finer UTC ``DateCreated`` ending in ``.000`` still meets the calendar guard."""
    observations = [
        iptc("2019-08-01T00:10:10", 540),
        xmp_created("2019-07-31T15:10:10.000", 0),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")


def test_other_xmp_capture_fields_are_unaffected() -> None:
    """Only the XMP original is a copy of the EXIF original; DateCreated is not."""
    observations = [
        dto("2019-08-01T00:10:10"),
        obs(
            Claim.CAPTURE_TIME,
            SourceKind.XMP,
            "xmp:http://ns.adobe.com/photoshop/1.0/:DateCreated#0",
            "2019-08-03T00:00:00",
            family="xmp",
        ),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")
    assert_that(decision.conflicts).is_equal_to((1,))


def test_undated_decision_names_read_issues() -> None:
    """An undated decision still says that some evidence is missing."""
    issue = ReadIssue(reader="xmp", status="failed", message="bad packet")

    decision = resolve_capture_date(
        evidence(hint(Claim.MODIFIED_TIME), issues=(issue,)),
        policy=policy(),
    )

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(decision.warnings).is_equal_to(("read-issues",))
