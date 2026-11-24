"""Tests for the capture-date resolver (#396).

Every input is an evidence set built in the test (no sample files). The
clock is fixed at 2026-11-14T00:00:00Z.
"""

from __future__ import annotations

import builtins
import itertools
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from assertpy import assert_that

from tests.media.date_evidence_support import (
    FILE,
    NOW,
    TAKEOUT_JSON,
    apple,
    container,
    device,
    dto,
    evidence,
    filename,
    gps,
    hint,
    iptc,
    obs,
    png,
    policy,
    reasons,
    semantic,
    sidecar_xmp,
    software,
    takeout,
    time_value,
    xmp_created,
)
from winnow.media import resolve_capture_date as exported_resolver
from winnow.media.resolve_date import (
    CAPTURE_RULES,
    RULESET_VERSION,
    ClockDefault,
    DatePolicy,
    resolve_capture_date,
    resolve_digitized_date,
)
from winnow.models.evidence import (
    Certainty,
    Claim,
    Decision,
    Observation,
    ParseStatus,
    Precision,
    ReadIssue,
    SourceKind,
)

Scenario = Callable[[], list[Observation]]

FIRES: dict[str, tuple[Scenario, Certainty]] = {
    "exif-original-offset": (
        lambda: [dto("2019-07-04T10:10:10", 540)],
        Certainty.PROBABLE,
    ),
    "quicktime-creationdate": (
        lambda: [apple("2019-07-04T10:10:10")],
        Certainty.PROBABLE,
    ),
    "exif-original-wallclock": (
        lambda: [dto("2019-07-04T10:10:10")],
        Certainty.PROBABLE,
    ),
    "iptc-xmp-date-created": (
        lambda: [iptc("2019-07-04T10:10:10", 540)],
        Certainty.PROBABLE,
    ),
    "gps-only": (lambda: [gps("2019-07-04T01:10:12")], Certainty.WEAK),
    "png-creation-time": (lambda: [png("2019-07-04T10:10:10")], Certainty.WEAK),
    "filename-pattern": (lambda: [filename("2019-07-04T10:10:10")], Certainty.WEAK),
}
"""Per rule: evidence where it is the only rule with input, and its certainty."""

AWAITING_ASSOCIATION: tuple[str, ...] = ("sidecar-takeout", "sidecar-xmp")
"""Rules that cannot fire until #402 records a verified unique sidecar
association; until then their inputs are hints (see the rulings tests)."""

PREEMPTED: dict[str, tuple[Scenario, str]] = {
    "quicktime-creationdate": (
        lambda: [
            apple("2019-07-04T10:10:10"),
            dto("2019-07-04T10:10:10", 540, source=SourceKind.XMP),
        ],
        "exif-original-offset",
    ),
    "exif-original-wallclock": (
        lambda: [dto("2019-07-04T10:10:10"), apple("2019-07-04T10:10:10")],
        "quicktime-creationdate",
    ),
    "iptc-xmp-date-created": (
        lambda: [xmp_created("2019-07-04"), dto("2019-07-04T10:10:10")],
        "exif-original-wallclock",
    ),
    "gps-only": (
        lambda: [gps("2019-07-04T01:10:12"), iptc("2019-07-04T10:10:10")],
        "iptc-xmp-date-created",
    ),
    "sidecar-takeout": (
        lambda: [takeout("2019-07-04T01:10:10"), iptc("2019-07-04T10:10:10", 540)],
        "iptc-xmp-date-created",
    ),
    "sidecar-xmp": (
        lambda: [sidecar_xmp("2019-07-04T10:10:10"), iptc("2019-07-04T10:10:10")],
        "iptc-xmp-date-created",
    ),
    "png-creation-time": (
        lambda: [png("2019-07-04T10:10:10"), iptc("2019-07-04T10:10:10")],
        "iptc-xmp-date-created",
    ),
    "filename-pattern": (
        lambda: [filename("2019-07-04T10:10:10"), png("2019-07-04T10:10:10")],
        "png-creation-time",
    ),
}
"""Per rule: its input plus an agreeing input of a higher rule, and the winner."""


@pytest.fixture
def default_policy() -> DatePolicy:
    """Provide the conservative default policy with the fixed clock.

    Returns:
        The policy.
    """
    return policy()


def test_every_rule_has_cases() -> None:
    """Each rule fires in a case or awaits #402; all but the first can be preempted."""
    rule_ids = [rule.rule_id for rule in CAPTURE_RULES]

    assert_that(sorted([*FIRES, *AWAITING_ASSOCIATION])).is_equal_to(sorted(rule_ids))
    assert_that(sorted(PREEMPTED)).is_equal_to(sorted(rule_ids[1:]))


@pytest.mark.parametrize("rule_id", list(FIRES), ids=list(FIRES))
def test_rule_fires(rule_id: str, default_policy: DatePolicy) -> None:
    """Each rule fires alone, with its certainty and the observation's value.

    Args:
        rule_id: The rule under test.
        default_policy: The default policy.
    """
    scenario, certainty = FIRES[rule_id]
    observations = scenario()

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to(rule_id)
    assert_that(decision.certainty).is_equal_to(certainty)
    assert_that(decision.value).is_equal_to(observations[0].value)
    assert_that(decision.chosen).is_equal_to((0,))
    assert_that(decision.rule_version).is_equal_to(RULESET_VERSION)


@pytest.mark.parametrize("rule_id", list(PREEMPTED), ids=list(PREEMPTED))
def test_rule_preempted(rule_id: str, default_policy: DatePolicy) -> None:
    """A higher rule's input pre-empts each rule.

    Args:
        rule_id: The pre-emptied rule.
        default_policy: The default policy.
    """
    scenario, winner = PREEMPTED[rule_id]
    observations = scenario()

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to(winner)
    assert_that(decision.value).is_equal_to(observations[1].value)
    assert_that(decision.conflicts).is_empty()


def test_gps_only_fires_only_alone(default_policy: DatePolicy) -> None:
    """A file-name date fires before GPS: GPS dates a file only when alone."""
    observations = [gps("2019-07-04T01:10:12"), filename("2019-07-04T10:10:10")]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("filename-pattern")
    assert_that(decision.incomparable).is_equal_to((0,))


def test_edit_time_and_mtime_only_is_undated(default_policy: DatePolicy) -> None:
    """IFD0 ``DateTime`` and ``st_mtime`` alone leave the file undated."""
    observations = [
        hint(Claim.MODIFIED_TIME, "2021-01-01T09:00:00"),
        hint(
            Claim.FILE_MODIFIED_TIME, "2021-01-01T09:00:00", offset_origin="utc-by-spec"
        ),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(decision.certainty).is_equal_to(Certainty.NONE)
    assert_that(decision.value).is_none()
    assert_that(reasons(decision)).is_equal_to({0: "not-capture", 1: "not-capture"})


def test_far_future_original_is_rejected(default_policy: DatePolicy) -> None:
    """``DateTimeOriginal=2099:12:31 00:00:00`` is ``future``."""
    decision = resolve_capture_date(
        evidence(dto("2099-12-31T00:00:00")),
        policy=default_policy,
    )

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(reasons(decision)).is_equal_to({0: "future"})


def test_naive_wall_later_today_is_not_future(default_policy: DatePolicy) -> None:
    """A naive 2026-11-14 13:00 may be in the past somewhere: kept."""
    decision = resolve_capture_date(
        evidence(dto("2026-11-14T13:00:00")),
        policy=default_policy,
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")


@pytest.mark.parametrize(
    ("value", "offset", "future"),
    [
        pytest.param("2026-11-14T14:00:01", None, True, id="wall-past-plus-14h"),
        pytest.param("2026-11-14T14:00:00", None, False, id="wall-at-plus-14h"),
        pytest.param("2026", None, False, id="year-containing-now"),
        pytest.param("2026-11-14T00:00", 0, False, id="minute-starting-at-now"),
        pytest.param("2026-11-14T00:01", 0, True, id="instant-minute-after-now"),
        pytest.param("2026-11-14T09:00:30", 540, True, id="instant-with-offset"),
    ],
)
def test_future_boundaries(
    value: str,
    offset: int | None,
    future: bool,
    default_policy: DatePolicy,
) -> None:
    """Future compares an earliest value at its precision, instants in UTC.

    Args:
        value: The capture value.
        offset: Its offset.
        future: Whether it is rejected as future.
        default_policy: The default policy.
    """
    decision = resolve_capture_date(evidence(dto(value, offset)), policy=default_policy)

    assert_that(reasons(decision).get(0) == "future").is_equal_to(future)


def test_explicit_offset_gives_probable_instant(default_policy: DatePolicy) -> None:
    """``10:10:10`` at ``+09:00`` is the instant 01:10:10Z, PROBABLE alone."""
    decision = resolve_capture_date(
        evidence(dto("2019-07-04T10:10:10", 540)),
        policy=default_policy,
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-offset")
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.value).is_not_none()
    instant = time_value(decision).instant()
    assert_that(instant).is_equal_to(datetime(2019, 7, 4, 1, 10, 10, tzinfo=UTC))
    assert_that(decision.warnings).is_empty()


def test_gps_two_seconds_off_does_not_confirm_by_default(
    default_policy: DatePolicy,
) -> None:
    """With zero GPS tolerance a fix two seconds off stays a hint."""
    observations = [dto("2019-07-04T10:10:10", 540), gps("2019-07-04T01:10:12")]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(reasons(decision)).is_equal_to({1: "gps-disagrees"})
    assert_that(decision.conflicts).is_empty()


def test_gps_agreement_confirms_under_explicit_tolerance() -> None:
    """An explicit GPS tolerance lets an independent fix confirm the instant."""
    observations = [dto("2019-07-04T10:10:10", 540), gps("2019-07-04T01:10:12")]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(gps_tolerance=timedelta(seconds=1)),
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-offset")
    assert_that(decision.certainty).is_equal_to(Certainty.CONFIRMED)
    assert_that(decision.chosen).is_equal_to((0, 1))


@pytest.mark.parametrize(
    ("tolerance", "certainty"),
    [
        pytest.param(
            timedelta(seconds=1), Certainty.CONFIRMED, id="gap-equals-tolerance"
        ),
        pytest.param(timedelta(milliseconds=999), Certainty.PROBABLE, id="gap-over"),
    ],
)
def test_gps_tolerance_boundary(tolerance: timedelta, certainty: Certainty) -> None:
    """A gap equal to the tolerance agrees; one microsecond more does not.

    Args:
        tolerance: The GPS tolerance.
        certainty: The expected certainty.
    """
    observations = [dto("2019-07-04T10:10:10", 540), gps("2019-07-04T01:10:12")]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(gps_tolerance=tolerance),
    )

    assert_that(decision.certainty).is_equal_to(certainty)


@pytest.mark.parametrize(
    "fix",
    [
        pytest.param("2019-07-04T01:10:12", id="contemporaneous"),
        pytest.param("2019-07-04T00:10:12", id="stale-one-hour"),
    ],
)
def test_naive_original_with_gps_stays_wallclock(fix: str) -> None:
    """GPS never gives a naive capture time a timezone.

    Args:
        fix: The GPS fix in UTC.
    """
    observations = [dto("2019-07-04T10:10:10"), gps(fix)]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(gps_tolerance=timedelta(hours=2)),
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")
    assert_that(decision.value).is_equal_to(observations[0].value)
    offset = time_value(decision).offset_minutes
    assert_that(offset).is_none()
    assert_that(decision.incomparable).is_equal_to((1,))
    assert_that(decision.warnings).contains("zone-unknown")


def test_apple_key_with_agreeing_container(default_policy: DatePolicy) -> None:
    """The Apple key dates the movie; an agreeing container time is a hint."""
    observations = [
        apple("2019-07-04T10:10:10"),
        container("2019-07-04T01:10:10"),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("quicktime-creationdate")
    assert_that(decision.value).is_equal_to(observations[0].value)
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(reasons(decision)).is_equal_to({1: "container-compatible"})


def test_container_two_days_later_is_a_hint(default_policy: DatePolicy) -> None:
    """A later container time is an encoding hint, not a capture conflict."""
    observations = [
        apple("2019-07-04T10:10:10"),
        container("2019-07-06T01:10:10"),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.value).is_equal_to(observations[0].value)
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.conflicts).is_empty()
    assert_that(reasons(decision)).is_equal_to({1: "container-differs"})


def test_conflicting_explicit_capture_key(default_policy: DatePolicy) -> None:
    """An explicit capture value that disagrees is a conflict, one level down."""
    observations = [
        dto("2019-07-04T10:10:10", 540, source=SourceKind.XMP),
        apple("2019-07-05T10:10:10"),
        container("2019-07-04T01:10:10"),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("exif-original-offset")
    assert_that(decision.conflicts).is_equal_to((1,))
    assert_that(decision.certainty).is_equal_to(Certainty.WEAK)


def test_container_alone_is_undated(default_policy: DatePolicy) -> None:
    """Container creation alone never dates a file."""
    decision = resolve_capture_date(
        evidence(container("2019-07-04T01:10:10")),
        policy=default_policy,
    )

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(reasons(decision)).is_equal_to({0: "not-capture"})


def test_digitized_a_year_apart_is_no_conflict(default_policy: DatePolicy) -> None:
    """Original and digitized from one EXIF block, a year apart, do not conflict."""
    observations = [
        dto("2019-07-04T10:10:10"),
        hint(Claim.DIGITIZED_TIME, "2020-07-04T10:10:10"),
    ]
    evidence_set = evidence(*observations)

    capture = resolve_capture_date(evidence_set, policy=default_policy)
    digitized = resolve_digitized_date(evidence_set, policy=default_policy)

    assert_that(capture.conflicts).is_empty()
    assert_that(capture.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(reasons(capture)).is_equal_to({1: "not-capture"})
    assert_that(digitized.rule_id).is_equal_to("digitized-wallclock")
    assert_that(digitized.value).is_equal_to(observations[1].value)
    assert_that(reasons(digitized)).is_equal_to({0: "not-digitized"})


def test_documented_reset_is_rejected() -> None:
    """A documented camera reset value is a placeholder."""
    reset = ClockDefault(
        make="Acme",
        model="Shooter 1",
        locator="ExifIFD:DateTimeOriginal#0",
        raw="2000:01:01 00:00:00",
    )
    observations = [
        device("Acme", "IFD0:Make#0"),
        device("Shooter 1", "IFD0:Model#0"),
        dto("2000-01-01T00:00:00", raw="2000:01:01 00:00:00"),
        container("2019-07-04T01:10:10"),
    ]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(known_clock_defaults=(reset,)),
    )

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(reasons(decision)).is_equal_to({2: "placeholder", 3: "not-capture"})


def test_reset_profile_needs_the_documented_model() -> None:
    """Another model's photograph at the same value is not rejected."""
    reset = ClockDefault(
        make="Acme",
        model="Shooter 1",
        locator="ExifIFD:DateTimeOriginal#0",
        raw="2000:01:01 00:00:00",
    )
    observations = [
        device("Acme", "IFD0:Make#0"),
        device("Shooter 2", "IFD0:Model#0"),
        dto("2000-01-01T00:00:00", raw="2000:01:01 00:00:00"),
    ]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(known_clock_defaults=(reset,)),
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")


def test_unprofiled_millennium_midnight_is_kept_as_suspicious(
    default_policy: DatePolicy,
) -> None:
    """Midnight on 2000-01-01 without a profile is kept, capped at WEAK."""
    decision = resolve_capture_date(
        evidence(dto("2000-01-01T00:00:00")),
        policy=default_policy,
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")
    assert_that(decision.certainty).is_equal_to(Certainty.WEAK)
    assert_that(decision.warnings).contains("suspicious-clock-default")


def test_scanned_print_has_undated_capture(default_policy: DatePolicy) -> None:
    """A scan with only a 2024 scan time: capture undated, digitized 2024."""
    observations = [hint(Claim.DIGITIZED_TIME, "2024-03-02T11:00:00")]
    evidence_set = evidence(*observations)

    capture = resolve_capture_date(evidence_set, policy=default_policy)
    digitized = resolve_digitized_date(evidence_set, policy=default_policy)

    assert_that(capture.rule_id).is_equal_to("undated")
    assert_that(digitized.value).is_equal_to(observations[0].value)


@pytest.mark.parametrize(
    ("observation", "rule_id", "certainty"),
    [
        pytest.param(
            dto("1940", source=SourceKind.XMP),
            "exif-original-wallclock",
            Certainty.PROBABLE,
            id="xmp-original",
        ),
        pytest.param(
            xmp_created("1940"),
            "iptc-xmp-date-created",
            Certainty.WEAK,
            id="date-created",
        ),
    ],
)
def test_manual_capture_year_keeps_year_precision(
    observation: Observation,
    rule_id: str,
    certainty: Certainty,
    default_policy: DatePolicy,
) -> None:
    """A capture year typed onto a scan stays a year, never 1 January.

    Args:
        observation: The year-only capture field.
        rule_id: The rule that reads it.
        certainty: Its certainty.
        default_policy: The default policy.
    """
    observations = [observation, hint(Claim.DIGITIZED_TIME, "2024-03-02T11:00:00")]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to(rule_id)
    assert_that(decision.certainty).is_equal_to(certainty)
    precision = time_value(decision).precision
    assert_that(precision).is_equal_to(Precision.YEAR)
    rendered = time_value(decision).render()
    assert_that(rendered).is_equal_to("1940")


def test_editor_resave_keeps_the_original(default_policy: DatePolicy) -> None:
    """An editor's later IFD0 and XMP modification dates remain hints."""
    observations = [
        dto("2019-07-04T10:10:10", 540),
        hint(Claim.MODIFIED_TIME, "2021-01-01T09:00:00"),
        obs(
            Claim.MODIFIED_TIME,
            SourceKind.XMP,
            "xmp:http://ns.adobe.com/xap/1.0/:ModifyDate#0",
            "2021-01-01T09:00:00",
            60,
            family="xmp",
        ),
        software("Adobe Photoshop 25.0 (Macintosh)"),
        gps("2019-07-04T01:10:10"),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.value).is_equal_to(observations[0].value)
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.warnings).contains("editing-software")
    assert_that(reasons(decision)).is_equal_to({1: "not-capture", 2: "not-capture"})


def test_editor_export_with_container_only_is_undated(
    default_policy: DatePolicy,
) -> None:
    """An export with only container creation and software is undated."""
    observations = [container("2023-05-01T08:00:00"), software("Lavf60.3.100")]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("undated")


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param((Claim.RECEIVED_TIME,), id="messenger-copy"),
        pytest.param(
            (
                Claim.FILE_CREATED_TIME,
                Claim.FILE_MODIFIED_TIME,
                Claim.FILE_CHANGED_TIME,
            ),
            id="backup-restore",
        ),
        pytest.param((Claim.UPLOADED_TIME, Claim.RELEASE_TIME), id="upload-release"),
    ],
)
def test_copies_without_capture_are_undated(
    claims: tuple[Claim, ...],
    default_policy: DatePolicy,
) -> None:
    """Arrival, filesystem, upload and release times never date a file.

    Args:
        claims: The only date claims the file carries.
        default_policy: The default policy.
    """
    observations = [hint(claim, "2025-02-03T04:05:06") for claim in claims]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(set(reasons(decision).values())).is_equal_to({"not-capture"})


def test_filename_date_is_weak_and_named(default_policy: DatePolicy) -> None:
    """``IMG_20190704_101010.jpg`` dates a file at WEAK under the default policy."""
    decision = resolve_capture_date(
        evidence(filename("2019-07-04T10:10:10")),
        policy=default_policy,
    )

    assert_that(decision.rule_id).is_equal_to("filename-pattern")
    assert_that(decision.certainty).is_equal_to(Certainty.WEAK)


def test_filename_date_is_a_hint_when_disabled() -> None:
    """With the file-name policy off, the name's date is a hint only."""
    decision = resolve_capture_date(
        evidence(filename("2019-07-04T10:10:10")),
        policy=policy(allow_filename_dates=False),
    )

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(reasons(decision)).is_equal_to({0: "filename-disabled"})


def test_same_rank_originals_conflict_in_every_order(
    default_policy: DatePolicy,
) -> None:
    """Two different originals, even in one family, conflict in any order."""
    observations = [
        dto("2019-07-04T10:10:10"),
        dto("2019-07-04T11:11:11", locator="ExifIFD:DateTimeOriginal#1"),
        filename("2019-07-04T10:10:10"),
    ]
    expected: dict[str, Any] | None = None

    for order in itertools.permutations(observations):
        evidence_set = evidence(*order)
        decision = resolve_capture_date(evidence_set, policy=default_policy)
        assert_that(decision.rule_id).is_equal_to("conflicting-capture")
        assert_that(decision.value).is_none()
        assert_that(decision.certainty).is_equal_to(Certainty.NONE)
        assert_that(decision.conflicts).is_length(2)
        found = semantic(decision, evidence_set)
        expected = expected or found
        assert_that(found).is_equal_to(expected)


def test_copied_xmp_does_not_raise_certainty(default_policy: DatePolicy) -> None:
    """A matching XMP copy is kept as provenance but confirms nothing."""
    observations = [
        dto("2019-07-04T10:10:10", 540),
        dto("2019-07-04T10:10:10", 540, source=SourceKind.XMP),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.chosen).is_length(2)


@pytest.mark.parametrize(
    ("other", "rule_id", "value_index"),
    [
        pytest.param(
            "2019-07-04T10:10:10.12", "exif-original-wallclock", 0, id="finer"
        ),
        pytest.param("2019-07-04T10:10", "exif-original-wallclock", 0, id="coarser"),
        pytest.param(
            "2019-07-04T10:10:11", "conflicting-capture", None, id="next-second"
        ),
    ],
)
def test_same_rank_values_compare_at_stated_precision(
    other: str,
    rule_id: str,
    value_index: int | None,
    default_policy: DatePolicy,
) -> None:
    """An overlapping XMP copy supports the EXIF value; adjacent seconds conflict.

    Args:
        other: The second original's text.
        rule_id: The expected rule.
        value_index: Which observation's value is chosen, if any.
        default_policy: The default policy.
    """
    observations = [
        dto("2019-07-04T10:10:10"),
        dto(other, source=SourceKind.XMP),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to(rule_id)
    expected = None if value_index is None else observations[value_index].value
    assert_that(decision.value).is_equal_to(expected)


@pytest.mark.parametrize(
    ("tolerance", "conflict"),
    [
        pytest.param(timedelta(0), True, id="default-zero"),
        pytest.param(timedelta(seconds=1), False, id="gap-equals-tolerance"),
        pytest.param(timedelta(microseconds=999_999), True, id="gap-over"),
    ],
)
def test_agreement_tolerance_boundary(tolerance: timedelta, conflict: bool) -> None:
    """A lower-rank embedded value two seconds later conflicts unless tolerated.

    Args:
        tolerance: The agreement tolerance.
        conflict: Whether the lower-rank value is a conflict.
    """
    observations = [dto("2019-07-04T10:10:10"), xmp_created("2019-07-04T10:10:12")]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(agreement_tolerance=tolerance),
    )

    assert_that(decision.value).is_equal_to(observations[0].value)
    assert_that(decision.conflicts == (1,)).is_equal_to(conflict)
    certainty = Certainty.WEAK if conflict else Certainty.PROBABLE
    assert_that(decision.certainty).is_equal_to(certainty)


def test_weak_winner_with_conflict_is_undated(default_policy: DatePolicy) -> None:
    """A WEAK value with a conflict drops to NONE and dates nothing."""
    observations = [xmp_created("2019"), png("2018-01-01T00:00:00")]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")
    assert_that(decision.value).is_none()
    assert_that(decision.conflicts).is_equal_to((0, 1))


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        pytest.param(ParseStatus.EMPTY, "placeholder", id="empty"),
        pytest.param(ParseStatus.PLACEHOLDER, "placeholder", id="all-zero"),
        pytest.param(ParseStatus.MALFORMED, "unparsed", id="malformed"),
        pytest.param(ParseStatus.UNSUPPORTED, "unparsed", id="unsupported"),
    ],
)
def test_unusable_values_are_rejected(
    status: ParseStatus,
    reason: str,
    default_policy: DatePolicy,
) -> None:
    """A field that did not parse is rejected with a stable code.

    Args:
        status: The parse status.
        reason: The expected code.
        default_policy: The default policy.
    """
    observation = obs(
        Claim.CAPTURE_TIME,
        SourceKind.EXIF,
        "ExifIFD:DateTimeOriginal#0",
        None,
        raw="0000:00:00 00:00:00",
        status=status,
    )

    decision = resolve_capture_date(evidence(observation), policy=default_policy)

    assert_that(reasons(decision)).is_equal_to({0: reason})


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        pytest.param(
            dto("2019-07-04T10:10:10", 0, offset_origin="utc-by-spec"),
            "offset-not-explicit",
            id="original-utc-by-spec",
        ),
        pytest.param(
            apple("2019-07-04T10:10:10", None),
            "offset-not-explicit",
            id="apple-key-without-offset",
        ),
        pytest.param(
            dto("2019-07-04T10:10:10", locator="ExifIFD:SomethingElse#0"),
            "no-rule",
            id="unknown-exif-field",
        ),
        pytest.param(
            obs(
                Claim.CAPTURE_TIME,
                SourceKind.SIDECAR_AAE,
                "aae:adjustmentTimestamp",
                "2019-07-04T10:10:10",
                origin=TAKEOUT_JSON.with_suffix(".AAE"),
            ),
            "sidecar-unverified",
            id="aae-timestamp",
        ),
        pytest.param(
            dto("2019-07-04T10:10:10", origin=FILE.with_name("other.jpg")),
            "foreign-origin",
            id="foreign-origin",
        ),
        pytest.param(
            obs(
                Claim.CAPTURE_TIME,
                SourceKind.EXIF,
                "ExifIFD:DateTimeOriginal#0",
                None,
                value="not a time",
            ),
            "unparsed",
            id="text-value",
        ),
    ],
)
def test_screening_codes(
    observation: Observation,
    reason: str,
    default_policy: DatePolicy,
) -> None:
    """Capture fields no rule can read are rejected with their code.

    Args:
        observation: The observation.
        reason: The expected code.
        default_policy: The default policy.
    """
    decision = resolve_capture_date(evidence(observation), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(reasons(decision)).is_equal_to({0: reason})


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param("0", "zero-container-time", id="integer-zero"),
        pytest.param("1970-01-01T00:00:00Z", "not-capture", id="demuxed-epoch"),
    ],
)
def test_unset_container_time(
    raw: str, reason: str, default_policy: DatePolicy
) -> None:
    """Only a direct integer 0 is an unset container time.

    Args:
        raw: The raw container value.
        reason: The expected code.
        default_policy: The default policy.
    """
    wall = "" if raw == "0" else "1970-01-01T00:00:00"
    status = ParseStatus.EMPTY if raw == "0" else ParseStatus.OK
    observation = container(wall, raw=raw, status=status)

    decision = resolve_capture_date(evidence(observation), policy=default_policy)

    assert_that(reasons(decision)).is_equal_to({0: reason})


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(software("Adobe Photoshop 25.0 (Macintosh)"), id="editor"),
        pytest.param(iptc("2019-07-04T10:10:10"), id="incomparable-other-field"),
    ],
)
def test_warnings_and_incomparable_block_confirmed(extra: Observation) -> None:
    """An editor or an incomparable capture field keeps GPS from confirming.

    Args:
        extra: The observation added to otherwise confirming evidence.
    """
    observations = [dto("2019-07-04T10:10:10", 540), gps("2019-07-04T01:10:10"), extra]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)


def test_exact_gps_agreement_confirms_by_default() -> None:
    """A GPS fix in the same second confirms without any tolerance."""
    observations = [dto("2019-07-04T10:10:10", 540), gps("2019-07-04T01:10:10")]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.CONFIRMED)


def test_read_issues_block_confirmed() -> None:
    """Missing evidence keeps a result from being CONFIRMED."""
    observations = [dto("2019-07-04T10:10:10", 540), gps("2019-07-04T01:10:10")]
    issue = ReadIssue(reader="xmp", status="failed", message="bad packet")

    decision = resolve_capture_date(
        evidence(*observations, issues=(issue,)),
        policy=policy(),
    )

    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.warnings).contains("read-issues")


def test_digitized_conflict(default_policy: DatePolicy) -> None:
    """Two different digitized times give no digitized date."""
    observations = [
        hint(Claim.DIGITIZED_TIME, "2024-03-02T11:00:00"),
        obs(
            Claim.DIGITIZED_TIME,
            SourceKind.XMP,
            "xmp:http://ns.adobe.com/xap/1.0/:CreateDate#0",
            "2024-03-09T11:00:00",
            family="xmp",
        ),
    ]

    decision = resolve_digitized_date(evidence(*observations), policy=default_policy)

    assert_that(decision.rule_id).is_equal_to("conflicting-digitized")
    assert_that(decision.claim).is_equal_to(Claim.DIGITIZED_TIME)


def test_reverse_order_gives_the_same_decision(default_policy: DatePolicy) -> None:
    """Reversing the observations changes indices, not the decision."""
    observations = [
        dto("2019-07-04T10:10:10", 540),
        gps("2019-07-04T01:10:10"),
        container("2019-07-04T01:10:10"),
        hint(Claim.FILE_MODIFIED_TIME, offset_origin="utc-by-spec"),
        filename("2019-07-04T10:10:10"),
    ]
    forward = evidence(*observations)
    backward = evidence(*reversed(observations))

    first = resolve_capture_date(forward, policy=default_policy)
    second = resolve_capture_date(backward, policy=default_policy)

    assert_that(semantic(second, backward)).is_equal_to(semantic(first, forward))
    assert_that(second.chosen).is_not_equal_to(first.chosen)


def test_decision_round_trips_as_json(default_policy: DatePolicy) -> None:
    """The decision serialises with the evidence model's ``Decision``."""
    decision = resolve_capture_date(
        evidence(dto("2019-07-04T10:10:10.123", 540), gps("2019-07-04T01:10:12")),
        policy=default_policy,
    )

    restored = Decision.model_validate_json(decision.model_dump_json())

    assert_that(restored).is_equal_to(decision)


def test_package_exports_the_resolver() -> None:
    """``winnow.media`` exports the resolver."""
    assert_that(exported_resolver).is_same_as(resolve_capture_date)


@pytest.mark.parametrize(
    ("now", "gps_tolerance", "agreement_tolerance", "message"),
    [
        pytest.param(
            datetime(2026, 11, 14),
            timedelta(0),
            timedelta(0),
            "aware",
            id="naive-now",
        ),
        pytest.param(NOW, timedelta(seconds=-1), timedelta(0), "gps", id="neg-gps"),
        pytest.param(
            NOW,
            timedelta(0),
            timedelta(seconds=-1),
            "agreement",
            id="neg-agreement",
        ),
    ],
)
def test_policy_rejects_invalid_values(
    now: datetime,
    gps_tolerance: timedelta,
    agreement_tolerance: timedelta,
    message: str,
) -> None:
    """A naive clock or a negative tolerance is refused.

    Args:
        now: The clock.
        gps_tolerance: The GPS tolerance.
        agreement_tolerance: The agreement tolerance.
        message: Text the error names.
    """
    with pytest.raises(ValueError, match=message):
        DatePolicy(
            now=now,
            gps_tolerance=gps_tolerance,
            agreement_tolerance=agreement_tolerance,
        )


def test_policy_accepts_any_aware_clock() -> None:
    """``now`` in another zone means the same instant."""
    tokyo = NOW.astimezone(timezone(timedelta(hours=9)))
    observations = [dto("2026-11-14T08:30:00", 540)]

    decision = resolve_capture_date(evidence(*observations), policy=policy(now=tokyo))

    assert_that(reasons(decision)).is_equal_to({})


@pytest.fixture
def host_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Switch the process timezone, restoring it afterwards.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.

    Yields:
        A function that sets ``TZ`` and applies it.
    """

    def apply(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield apply
    monkeypatch.undo()
    time.tzset()


def test_resolver_independent_of_tz(
    host_zone: Callable[[str], None],
    default_policy: DatePolicy,
) -> None:
    """The decision is identical under UTC and Los Angeles host zones.

    Args:
        host_zone: Sets the process timezone.
        default_policy: The default policy.
    """
    evidence_set = evidence(
        dto("2019-07-04T10:10:10"),
        dto("2019-07-04T10:10:10", 540, source=SourceKind.XMP),
        gps("2019-07-04T01:10:12"),
        hint(Claim.FILE_MODIFIED_TIME, offset_origin="utc-by-spec"),
    )
    results = []
    for zone in ("UTC", "America/Los_Angeles"):
        host_zone(zone)
        results.append(resolve_capture_date(evidence_set, policy=default_policy))

    assert_that(results[1]).is_equal_to(results[0])


def test_resolver_does_no_io(
    monkeypatch: pytest.MonkeyPatch,
    default_policy: DatePolicy,
) -> None:
    """Resolving opens and stats nothing.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        default_policy: The default policy.
    """
    evidence_set = evidence(
        dto("2019-07-04T10:10:10", 540),
        gps("2019-07-04T01:10:10"),
        takeout("2019-07-04T01:10:10"),
        container("2019-07-04T01:10:10"),
    )

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the resolver must not touch the filesystem")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(os, "stat", refuse)
    capture = resolve_capture_date(evidence_set, policy=default_policy)
    digitized = resolve_digitized_date(evidence_set, policy=default_policy)
    monkeypatch.undo()

    assert_that(capture.rule_id).is_equal_to("exif-original-offset")
    assert_that(digitized.rule_id).is_equal_to("undated")


@pytest.mark.parametrize(
    ("other", "conflict"),
    [
        pytest.param("2019-12-31T23:59:59", False, id="last-second-of-month"),
        pytest.param("2020-01-01T00:00:00", True, id="next-month-and-year"),
    ],
)
def test_month_span_ends_at_the_next_month(
    other: str,
    conflict: bool,
    default_policy: DatePolicy,
) -> None:
    """A month-precision original spans the whole month, December included.

    Args:
        other: A lower-rank XMP ``DateCreated`` value.
        conflict: Whether it conflicts.
        default_policy: The default policy.
    """
    observations = [dto("2019-12"), xmp_created(other)]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(decision.conflicts == (1,)).is_equal_to(conflict)


def test_span_at_the_end_of_time_is_clamped(default_policy: DatePolicy) -> None:
    """A container time in the last representable minute is compared safely."""
    observations = [dto("2019-07-04T10:10:10", 540), container("9999-12-31T23:59")]

    decision = resolve_capture_date(evidence(*observations), policy=default_policy)

    assert_that(reasons(decision)).is_equal_to({1: "container-differs"})


def test_wall_future_check_near_the_end_of_time() -> None:
    """A clock too late to add 14 hours to rejects no wall time as future."""
    late = datetime(9999, 12, 31, 20, tzinfo=UTC)

    decision = resolve_capture_date(
        evidence(dto("9999-12-31T23:00:00")),
        policy=policy(now=late),
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")


@pytest.mark.parametrize(
    "observation",
    [
        pytest.param(
            dto(
                "2019-07-04T10:10:10",
                source=SourceKind.XMP,
                locator="xmp:http://ns.adobe.com/xap/1.0/:Label#0",
            ),
            id="xmp-unknown-field",
        ),
        pytest.param(
            obs(
                Claim.DIGITIZED_TIME,
                SourceKind.QUICKTIME,
                "QuickTime:Keys:digitized#0",
                "2019-07-04T10:10:10",
            ),
            id="digitized-from-quicktime",
        ),
    ],
)
def test_fields_no_rule_reads(
    observation: Observation, default_policy: DatePolicy
) -> None:
    """A field no rule of the ruleset reads is ``no-rule`` in its own decision.

    Args:
        observation: The observation.
        default_policy: The default policy.
    """
    resolve = (
        resolve_digitized_date
        if observation.claim is Claim.DIGITIZED_TIME
        else resolve_capture_date
    )

    decision = resolve(evidence(observation), policy=default_policy)

    assert_that(reasons(decision)).is_equal_to({0: "no-rule"})


def test_reset_profile_ignores_unparsed_device_names() -> None:
    """Only parsed make and model names select a reset profile."""
    reset = ClockDefault(
        make="Acme",
        model="Shooter 1",
        locator="ExifIFD:DateTimeOriginal#0",
        raw="2000:01:01 00:00:00",
    )
    unparsed_make = obs(
        Claim.DEVICE,
        SourceKind.EXIF,
        "IFD0:Make#0",
        None,
        raw="Acme",
        status=ParseStatus.MALFORMED,
    )
    model_from_raw = obs(
        Claim.DEVICE,
        SourceKind.EXIF,
        "IFD0:Model#0",
        None,
        raw="Shooter 1\x00",
    )
    observations = [
        unparsed_make,
        model_from_raw,
        dto("2000-01-01T00:00:00", raw="2000:01:01 00:00:00"),
    ]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(known_clock_defaults=(reset,)),
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")
