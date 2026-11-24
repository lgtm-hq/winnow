"""Tests for the review rulings on the capture-date resolver (#396, PR #486).

Each test pins one ruling: the same property written at two precisions,
equivalent instants, sidecar association, stale evidence, file-name dates,
reset-like values against a contradiction, the editor list, the zero
container time, GPS independence and the reset profile's fields.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from assertpy import assert_that

from tests.media.date_evidence_support import (
    XMP_SIDECAR,
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
    sidecar_xmp,
    software,
    takeout,
    time_value,
    xmp_created,
)
from winnow.media._date_fields import field_kind
from winnow.media._date_policy import FieldKind
from winnow.media.resolve_date import ClockDefault, resolve_capture_date
from winnow.models.evidence import (
    Certainty,
    Claim,
    Observation,
    ParseStatus,
    ReadIssue,
    SourceKind,
)


@pytest.mark.parametrize(
    "observations",
    [
        pytest.param(
            [iptc("2019-07-04T10:10:10", 540), xmp_created("2019-07-04")],
            id="iptc-time-xmp-day",
        ),
        pytest.param(
            [
                obs(
                    Claim.CAPTURE_TIME,
                    SourceKind.IPTC,
                    "IPTC:DateCreated#0",
                    "2019-07-04",
                    family="iptc",
                ),
                xmp_created("2019-07-04T10:10:10", 540),
            ],
            id="iptc-day-xmp-time",
        ),
    ],
)
def test_same_property_at_day_and_timestamp_is_compatible(
    observations: list[Observation],
) -> None:
    """IPTC and XMP ``DateCreated`` for one day keep the fuller timestamp.

    Args:
        observations: The two representations of the property.
    """
    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("iptc-xmp-date-created")
    assert_that(time_value(decision).render()).is_equal_to("2019-07-04T10:10:10+09:00")
    assert_that(decision.chosen).is_length(2)
    assert_that(decision.conflicts).is_empty()


def test_same_property_on_another_day_still_abstains() -> None:
    """A date-only value for a different day is a real same-rank contradiction."""
    observations = [iptc("2019-07-04T10:10:10", 540), xmp_created("2019-07-05")]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")


def test_unlinked_wall_and_instant_of_one_rank_abstain() -> None:
    """Two PNG creation times, one zoned and one not, cannot be ordered."""
    observations = [
        png("2019-07-04T10:10:10"),
        obs(
            Claim.CAPTURE_TIME,
            SourceKind.PNG_TEXT,
            "png:iTXt:Creation Time#0",
            "2019-07-04T10:10:10",
            540,
            family="png-text",
        ),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")


@pytest.mark.parametrize(
    ("exif_text", "xmp_text"),
    [
        pytest.param("2019-07-04T10:10:10", "2019-07-04T01:10:10", id="same-day"),
        pytest.param("2019-08-01T00:10:10", "2019-07-31T15:10:10", id="month-boundary"),
    ],
)
def test_equivalent_instants_keep_the_camera_value(
    exif_text: str,
    xmp_text: str,
) -> None:
    """An offset original and its UTC copy are one value: the camera's.

    Args:
        exif_text: The EXIF original, written at +09:00.
        xmp_text: The XMP copy of the same instant, written in UTC.
    """
    observations = [
        dto(xmp_text, 0, source=SourceKind.XMP),
        dto(exif_text, 540),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("exif-original-offset")
    assert_that(decision.value).is_equal_to(observations[1].value)
    assert_that(decision.chosen).is_length(2)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("2019-07-04T10:10:10", "2019-07-04T10:10:11.5", id="unequal"),
        pytest.param("2019-07-04T10:10:10", "2019-07-04T10:10:11", id="equal"),
    ],
)
def test_same_rank_agreement_through_tolerance_alone_abstains(
    first: str,
    second: str,
) -> None:
    """Same-rank values that only tolerance joins are not one value.

    Args:
        first: An IPTC ``DateCreated``.
        second: An XMP ``DateCreated`` whose span does not overlap it.
    """
    observations = [iptc(first), xmp_created(second)]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(agreement_tolerance=timedelta(seconds=1)),
    )

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")


@pytest.mark.parametrize(
    "observations",
    [
        pytest.param([takeout("2019-07-04T01:10:10")], id="takeout"),
        pytest.param([sidecar_xmp("2019-07-04T10:10:10")], id="xmp-shared-stem"),
        pytest.param(
            [
                sidecar_xmp("2019-07-04T10:10:10"),
                obs(
                    Claim.MODIFIED_TIME,
                    SourceKind.SIDECAR_XMP,
                    "xmp:http://ns.adobe.com/xap/1.0/:ModifyDate#0",
                    "2020-01-01T00:00:00",
                    family="xmp-sidecar",
                    origin=XMP_SIDECAR.with_name("IMG_0001.JPG.xmp"),
                ),
            ],
            id="second-sidecar-edit-only",
        ),
    ],
)
def test_sidecar_dates_are_hints_until_association_is_verified(
    observations: list[Observation],
) -> None:
    """Without #402's verified unique association a sidecar dates nothing.

    Args:
        observations: Sidecar evidence recorded as dependencies.
    """
    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(reasons(decision)[0]).is_equal_to("sidecar-unverified")


def test_sidecar_never_suppresses_an_embedded_date() -> None:
    """A sidecar beside an embedded original is a hint; the original stands."""
    observations = [
        dto("2019-07-04T10:10:10"),
        sidecar_xmp("2021-01-01T00:00:00"),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")
    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(reasons(decision)).is_equal_to({1: "sidecar-unverified"})


def test_changed_file_is_undated_as_stale() -> None:
    """Observations of a previous file state never date its replacement."""
    observations = [dto("2019-07-04T10:10:10", 540), hint(Claim.MODIFIED_TIME)]
    issue = ReadIssue(reader="winnow", status="file-changed", message="mtime_ns")

    decision = resolve_capture_date(
        evidence(*observations, issues=(issue,)),
        policy=policy(),
    )

    assert_that(decision.rule_id).is_equal_to("undated")
    assert_that(decision.value).is_none()
    assert_that(decision.warnings).contains("stale-evidence")
    assert_that(reasons(decision)).is_equal_to(
        {0: "stale-evidence", 1: "stale-evidence"},
    )


def test_failed_optional_reader_still_dates() -> None:
    """A failed reader is not stale evidence: the date stands, unconfirmed."""
    issue = ReadIssue(reader="xmp", status="failed", message="bad packet")

    decision = resolve_capture_date(
        evidence(dto("2019-07-04T10:10:10", 540), issues=(issue,)),
        policy=policy(),
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-offset")
    assert_that(decision.warnings).contains("read-issues")


@pytest.mark.parametrize(
    ("embedded", "name", "certainty"),
    [
        pytest.param(
            png("2019-07-04T10:10:10"),
            "2019-07-05T10:10:10",
            Certainty.WEAK,
            id="export-name-on-png",
        ),
        pytest.param(
            dto("2019-08-01T00:10:10"),
            "2019-07-31T15:10:10",
            Certainty.PROBABLE,
            id="pixel-style-name",
        ),
    ],
)
def test_filename_never_vetoes_an_embedded_date(
    embedded: Observation,
    name: str,
    certainty: Certainty,
) -> None:
    """A disagreeing file name is a hint; the embedded date keeps its grade.

    Args:
        embedded: The embedded capture field.
        name: The file name's date.
        certainty: The embedded rule's certainty.
    """
    observations = [embedded, filename(name)]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.value).is_equal_to(embedded.value)
    assert_that(decision.certainty).is_equal_to(certainty)
    assert_that(decision.conflicts).is_empty()
    assert_that(reasons(decision)).is_equal_to({1: "filename-differs"})


def test_reset_like_value_contradicted_is_undated() -> None:
    """A reset-like midnight contradicted by another capture field dates nothing."""
    observations = [dto("2000-01-01T00:00:00"), xmp_created("2024-07-09T10:00:00")]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")
    assert_that(decision.value).is_none()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("10.0.2", id="iphone-7"),
        pytest.param("HDR+ 1.0.570503588zd", id="pixel"),
        pytest.param("00WW_3_380_SP02", id="nokia"),
        pytest.param("Lavf60.3.100", id="muxer"),
    ],
)
def test_camera_firmware_does_not_block_confirmed(text: str) -> None:
    """Only a known editing program blocks CONFIRMED, not camera firmware.

    Args:
        text: The ``Software`` value.
    """
    observations = [
        dto("2019-07-04T10:10:10", 540),
        gps("2019-07-04T01:10:10"),
        software(text),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.CONFIRMED)
    assert_that(decision.warnings).does_not_contain("editing-software")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Adobe Photoshop 7.0", id="photoshop"),
        pytest.param(
            "Adobe Photoshop Lightroom Classic 13.0 (Macintosh)",
            id="lightroom",
        ),
        pytest.param("GIMP 2.10.36", id="gimp"),
        pytest.param("darktable 4.6.1", id="darktable"),
        pytest.param("RawTherapee 5.10", id="rawtherapee"),
    ],
)
def test_known_editor_blocks_confirmed(text: str) -> None:
    """A known editor's name keeps an exact GPS match from confirming.

    Args:
        text: The ``Software`` value.
    """
    observations = [
        dto("2019-07-04T10:10:10", 540),
        gps("2019-07-04T01:10:10"),
        software(text),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)
    assert_that(decision.warnings).contains("editing-software")


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(filename("2019-07-04T10:10:10"), id="camera-filename"),
        pytest.param(
            dto("2019-07-04T10:10:10", source=SourceKind.XMP),
            id="naive-xmp-copy",
        ),
    ],
)
def test_agreeing_copies_do_not_block_confirmed(extra: Observation) -> None:
    """A camera file name or a naive XMP copy of the original does not block.

    Args:
        extra: The added observation.
    """
    observations = [
        dto("2019-07-04T10:10:10", 540),
        gps("2019-07-04T01:10:10"),
        extra,
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.CONFIRMED)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param("0", "zero-container-time", id="integer-zero"),
        pytest.param("00", "not-capture", id="padded-zero"),
    ],
)
def test_zero_container_time_is_named_for_what_was_read(raw: str, reason: str) -> None:
    """A container time read as the integer 0 is a zero container time.

    Args:
        raw: The raw container value.
        reason: The expected code.
    """
    observation = container("", raw=raw, status=ParseStatus.EMPTY)

    decision = resolve_capture_date(evidence(observation), policy=policy())

    assert_that(reasons(decision)).is_equal_to({0: reason})


def test_gps_in_the_original_family_does_not_confirm() -> None:
    """A GPS value from the original's own provenance family is no witness."""
    observations = [
        dto("2019-07-04T10:10:10", 540),
        gps("2019-07-04T01:10:10").model_copy(update={"family": "exif"}),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.PROBABLE)


def test_reset_profile_reads_make_and_model_fields_separately() -> None:
    """A make written in the model field does not select a reset profile."""
    reset = ClockDefault(
        make="Acme",
        model="Shooter 1",
        locator="ExifIFD:DateTimeOriginal#0",
        raw="2000:01:01 00:00:00",
    )
    observations = [
        device("Shooter 1", "IFD0:Make#0"),
        device("Acme", "IFD0:Model#0"),
        dto("2000-01-01T00:00:00", raw="2000:01:01 00:00:00"),
    ]

    decision = resolve_capture_date(
        evidence(*observations),
        policy=policy(known_clock_defaults=(reset,)),
    )

    assert_that(decision.rule_id).is_equal_to("exif-original-wallclock")


def test_one_instant_in_two_zones_from_one_block_abstains() -> None:
    """Two originals of one block stating one instant in two zones abstain."""
    observations = [
        dto("2019-07-04T10:10:10", 540),
        dto("2019-07-04T03:10:10", 120, locator="ExifIFD:DateTimeOriginal#1"),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.rule_id).is_equal_to("conflicting-capture")


def test_unparsed_editor_name_does_not_block_confirmed() -> None:
    """Only a parsed ``Software`` value can name an editor."""
    observations = [
        dto("2019-07-04T10:10:10", 540),
        gps("2019-07-04T01:10:10"),
        obs(
            Claim.SOFTWARE,
            SourceKind.EXIF,
            "IFD0:Software#0",
            None,
            raw="Adobe Photoshop 7.0",
            status=ParseStatus.MALFORMED,
        ),
    ]

    decision = resolve_capture_date(evidence(*observations), policy=policy())

    assert_that(decision.certainty).is_equal_to(Certainty.CONFIRMED)


@pytest.mark.parametrize(
    ("observation", "kind"),
    [
        pytest.param(
            sidecar_xmp("2019-07-04T10:10:10"), FieldKind.SIDECAR_XMP, id="xmp-original"
        ),
        pytest.param(
            obs(
                Claim.CAPTURE_TIME,
                SourceKind.SIDECAR_XMP,
                "xmp:http://ns.adobe.com/photoshop/1.0/:DateCreated#0",
                "2019-07-04",
                family="xmp-sidecar",
            ),
            FieldKind.SIDECAR_XMP,
            id="xmp-date-created",
        ),
        pytest.param(takeout("2019-07-04T01:10:10"), FieldKind.TAKEOUT, id="takeout"),
        pytest.param(
            obs(
                Claim.CAPTURE_TIME,
                SourceKind.QUICKTIME,
                "QuickTime:Keys:com.apple.quicktime.location.date#0",
                "2019-07-04T10:10:10",
                540,
                family="quicktime-keys",
            ),
            None,
            id="other-quicktime-key",
        ),
    ],
)
def test_sidecar_fields_keep_their_rule_for_402(
    observation: Observation,
    kind: FieldKind | None,
) -> None:
    """Sidecar fields still map to rules 8 and 9 for when #402 verifies them.

    Args:
        observation: The observation.
        kind: Its expected field kind.
    """
    assert_that(field_kind(observation)).is_equal_to(kind)
