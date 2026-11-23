"""Property tests for the capture-date resolver (#396).

Reordering the observations, or adding one that is not a capture time,
never changes what the resolver decides; only the indices move.
"""

from __future__ import annotations

from typing import Final

from assertpy import assert_that
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.media.date_evidence_support import (
    apple,
    container,
    dto,
    evidence,
    filename,
    gps,
    hint,
    iptc,
    png,
    policy,
    semantic,
    software,
    takeout,
)
from winnow.media.resolve_date import resolve_capture_date
from winnow.models.evidence import Claim, Observation, SourceKind

_MAX_EXAMPLES: Final[int] = 150

SCENARIOS: Final[tuple[tuple[Observation, ...], ...]] = (
    (
        dto("2019-07-04T10:10:10", 540),
        dto("2019-07-04T10:10:10", 540, source=SourceKind.XMP),
        gps("2019-07-04T01:10:10"),
        filename("2019-07-04T10:10:10"),
        software("17.1"),
    ),
    (
        dto("2019-07-04T10:10:10"),
        dto("2019-07-04T11:11:11", locator="ExifIFD:DateTimeOriginal#1"),
        iptc("2019-07-04T10:10:10"),
        hint(Claim.DIGITIZED_TIME),
    ),
    (
        apple("2019-07-04T10:10:10"),
        apple("2019-07-04T10:10:10.5", occurrence=1),
        container("2019-07-06T01:10:10"),
        gps("2019-07-04T00:10:10"),
    ),
    (
        png("2019-07-04T10:10:10"),
        filename("2018-01-01T00:00:00"),
        takeout("2019-07-04T01:10:10"),
    ),
    (
        dto("2099-12-31T00:00:00"),
        dto("2000-01-01T00:00:00", source=SourceKind.XMP),
        hint(Claim.MODIFIED_TIME),
    ),
)
"""Evidence mixes covering agreement, conflicts, hints and screening."""

EXTRAS: Final[tuple[Observation, ...]] = tuple(
    hint(claim, "2019-07-04T10:10:10", offset_origin="utc-by-spec")
    for claim in (
        Claim.MODIFIED_TIME,
        Claim.DIGITIZED_TIME,
        Claim.FILE_CREATED_TIME,
        Claim.FILE_MODIFIED_TIME,
        Claim.FILE_CHANGED_TIME,
        Claim.RECEIVED_TIME,
        Claim.UPLOADED_TIME,
        Claim.RELEASE_TIME,
        Claim.RECORDING_TIME,
    )
) + (container("2019-07-04T01:10:10"),)
"""Observations of claims that are not capture times."""


@st.composite
def _shuffled(draw: st.DrawFn) -> tuple[int, list[Observation], Observation | None]:
    """Draw a scenario, an order of its observations and maybe an extra one.

    Args:
        draw: Hypothesis's draw function.

    Returns:
        The scenario index, the shuffled observations with the extra
        inserted somewhere, and the extra (or ``None``).
    """
    index = draw(st.integers(min_value=0, max_value=len(SCENARIOS) - 1))
    order = list(draw(st.permutations(SCENARIOS[index])))
    extra = draw(st.none() | st.sampled_from(EXTRAS))
    if extra is not None:
        order.insert(draw(st.integers(min_value=0, max_value=len(order))), extra)
    return index, order, extra


@given(case=_shuffled())
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_order_and_non_capture_never_change_the_decision(
    case: tuple[int, list[Observation], Observation | None],
) -> None:
    """Shuffling, or adding a non-capture observation, keeps the decision.

    Args:
        case: Scenario index, shuffled observations and the added extra.
    """
    index, observations, extra = case
    reference_set = evidence(*SCENARIOS[index])
    shuffled_set = evidence(*observations)

    reference = semantic(
        resolve_capture_date(reference_set, policy=policy()),
        reference_set,
    )
    found = semantic(resolve_capture_date(shuffled_set, policy=policy()), shuffled_set)

    if extra is not None:
        rejected = found["rejected"]
        extra_rows = [row for row in rejected if row[0] == extra.id]
        assert_that(extra_rows).is_length(1)
        found["rejected"] = [row for row in rejected if row[0] != extra.id]
    assert_that(found).is_equal_to(reference)
