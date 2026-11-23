"""Select, compare and grade for the date resolver (#396).

Steps 2 to 4 of :mod:`winnow.media.resolve_date`: find the first rule that
fires and its single value, place every other date observation relative to
that value, and grade the result.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Final

from winnow.media._date_policy import (
    BLOCKS_CONFIRMED,
    DatePolicy,
    DateRule,
    FieldKind,
    Reason,
    WarningCode,
)
from winnow.media._date_screen import Screened
from winnow.media._date_spans import (
    Agreement,
    Span,
    compare,
    fineness,
    is_suspicious_default,
    span_of,
)
from winnow.models.evidence import (
    Certainty,
    Claim,
    EvidenceSet,
    ParseStatus,
    Precision,
    TimeValue,
)

_COARSER_THAN_DAY: Final[frozenset[Precision]] = frozenset(
    {Precision.YEAR, Precision.MONTH},
)

_LOWER: Final[dict[Certainty, Certainty]] = {
    Certainty.CONFIRMED: Certainty.PROBABLE,
    Certainty.PROBABLE: Certainty.WEAK,
    Certainty.WEAK: Certainty.NONE,
    Certainty.NONE: Certainty.NONE,
}

_CONTAINER_HINTS: Final[dict[Agreement, Reason]] = {
    Agreement.AGREE: Reason.CONTAINER_COMPATIBLE,
    Agreement.DIFFER: Reason.CONTAINER_DIFFERS,
    Agreement.INCOMPARABLE: Reason.CONTAINER_INCOMPARABLE,
}


@dataclass(slots=True)
class Outcome:
    """Where each date observation ended up, while a decision is built."""

    chosen: list[Screened] = field(default_factory=list)
    conflicts: list[Screened] = field(default_factory=list)
    incomparable: list[Screened] = field(default_factory=list)
    rejected: list[tuple[Screened, Reason]] = field(default_factory=list)
    gps_agrees: bool = False


def rule_of(entry: Screened) -> DateRule:
    """Return an eligible entry's rule.

    Args:
        entry: A screened entry that reached a rule.

    Returns:
        Its rule.

    Raises:
        ValueError: If the entry reached no rule.
    """
    if entry.rule is None:
        raise ValueError("entry reached no rule")
    return entry.rule


def value_of(entry: Screened) -> TimeValue:
    """Return an eligible entry's value.

    Args:
        entry: A screened entry that reached a rule.

    Returns:
        Its time value.

    Raises:
        ValueError: If the entry has no value.
    """
    if entry.value is None:
        raise ValueError("entry has no time value")
    return entry.value


def top_candidates(screened: Sequence[Screened]) -> list[Screened]:
    """Return the eligible entries of the first rule that fires.

    A rule marked ``alone`` (``gps-only``) fires only when every eligible
    entry is of that rule.

    Args:
        screened: Every screened entry.

    Returns:
        The entries of the firing rule; empty when none fires.
    """
    eligible = [entry for entry in screened if entry.rule is not None]
    pool = [entry for entry in eligible if not rule_of(entry).alone] or eligible
    if not pool:
        return []
    first = min(rule_of(entry).order for entry in pool)
    return [entry for entry in pool if rule_of(entry).order == first]


def single_value(top: Sequence[Screened], *, policy: DatePolicy) -> TimeValue | None:
    """Return the one value the firing rule's candidates give.

    Args:
        top: The firing rule's entries.
        policy: The resolver policy.

    Returns:
        The finest value when every pair of candidates agrees and exactly
        one distinct value is finest; ``None`` otherwise, so a tie is never
        broken by input order or by the timestamps.
    """
    spans = [span_of(value_of(entry)) for entry in top]
    for first, second in combinations(spans, 2):
        if compare(first, second, tolerance=policy.agreement_tolerance) is not (
            Agreement.AGREE
        ):
            return None
    finest = max(fineness(value_of(entry)) for entry in top)
    distinct: list[TimeValue] = []
    for entry in top:
        value = value_of(entry)
        if fineness(value) == finest and value not in distinct:
            distinct.append(value)
    return distinct[0] if len(distinct) == 1 else None


def compare_rest(
    screened: Sequence[Screened],
    *,
    top: Sequence[Screened],
    value: TimeValue,
    policy: DatePolicy,
    claim: Claim,
) -> Outcome:
    """Place every entry relative to the chosen value.

    Args:
        screened: Every screened entry.
        top: The firing rule's entries.
        value: The chosen value.
        policy: The resolver policy.
        claim: The claim decided.

    Returns:
        The outcome of each entry.
    """
    chosen_span = span_of(value)
    outcome = Outcome(chosen=list(top))
    for entry in screened:
        if entry in top:
            continue
        if entry.rule is None:
            outcome.rejected.append(
                (entry, _rejection_reason(entry, chosen_span, policy, claim)),
            )
            continue
        _place_eligible(entry, chosen_span=chosen_span, policy=policy, outcome=outcome)
    return outcome


def _place_eligible(
    entry: Screened,
    *,
    chosen_span: Span,
    policy: DatePolicy,
    outcome: Outcome,
) -> None:
    """Place an eligible entry of a lower rule.

    Args:
        entry: The entry.
        chosen_span: The chosen value's span.
        policy: The resolver policy.
        outcome: Updated in place.
    """
    is_gps = rule_of(entry).kind is FieldKind.GPS
    tolerance = policy.gps_tolerance if is_gps else policy.agreement_tolerance
    agreement = compare(chosen_span, span_of(value_of(entry)), tolerance=tolerance)
    if agreement is Agreement.AGREE:
        outcome.chosen.append(entry)
        outcome.gps_agrees = outcome.gps_agrees or is_gps
    elif agreement is Agreement.INCOMPARABLE:
        outcome.incomparable.append(entry)
    elif is_gps:
        outcome.rejected.append((entry, Reason.GPS_DISAGREES))
    else:
        outcome.conflicts.append(entry)


def _rejection_reason(
    entry: Screened,
    chosen_span: Span,
    policy: DatePolicy,
    claim: Claim,
) -> Reason:
    """Name a rejected entry's outcome, comparing container creation.

    Args:
        entry: A rejected entry.
        chosen_span: The chosen value's span.
        policy: The resolver policy.
        claim: The claim decided.

    Returns:
        A ``container-*`` code for a parsed container creation time in a
        capture decision, otherwise the screening code.
    """
    reason = entry.reason or Reason.NO_RULE
    observation = entry.observation
    if (
        claim is Claim.CAPTURE_TIME
        and reason is Reason.NOT_CAPTURE
        and observation.claim is Claim.CONTAINER_CREATED_TIME
        and observation.status is ParseStatus.OK
        and entry.value is not None
    ):
        agreement = compare(
            chosen_span,
            span_of(entry.value),
            tolerance=policy.agreement_tolerance,
        )
        return _CONTAINER_HINTS[agreement]
    return reason


def warnings_for(
    evidence: EvidenceSet,
    *,
    value: TimeValue,
    top: Iterable[Screened],
) -> frozenset[WarningCode]:
    """Collect the warnings for a decision with a value.

    Args:
        evidence: The file's evidence.
        value: The chosen value.
        top: The firing rule's entries.

    Returns:
        The warning codes.
    """
    found: set[WarningCode] = set()
    if value.offset_minutes is None:
        found.add(WarningCode.ZONE_UNKNOWN)
    if any(is_suspicious_default(value_of(entry)) for entry in top):
        found.add(WarningCode.SUSPICIOUS_CLOCK_DEFAULT)
    if any(
        observation.claim is Claim.SOFTWARE and observation.status is ParseStatus.OK
        for observation in evidence.observations
    ):
        found.add(WarningCode.SOFTWARE_RECORDED)
    if evidence.issues:
        found.add(WarningCode.READ_ISSUES)
    return frozenset(found)


def grade(
    rule: DateRule,
    *,
    value: TimeValue,
    outcome: Outcome,
    warnings: frozenset[WarningCode],
) -> Certainty:
    """Grade a decision with a value.

    Args:
        rule: The firing rule.
        value: The chosen value.
        outcome: Where every entry ended up.
        warnings: The decision's warnings.

    Returns:
        The rule's certainty (WEAK for a coarse value where the rule says
        so), one level lower with a conflict, capped at WEAK for a
        suspicious clock default, and CONFIRMED only for a confirmable
        rule with an agreeing GPS fix and nothing against it.
    """
    certainty = rule.certainty
    if rule.weak_when_coarse and value.precision in _COARSER_THAN_DAY:
        certainty = Certainty.WEAK
    if outcome.conflicts:
        certainty = _LOWER[certainty]
    if certainty is Certainty.NONE:
        return certainty
    if WarningCode.SUSPICIOUS_CLOCK_DEFAULT in warnings:
        return Certainty.WEAK
    if (
        rule.confirmable
        and certainty is Certainty.PROBABLE
        and outcome.gps_agrees
        and not outcome.conflicts
        and not outcome.incomparable
        and not warnings & BLOCKS_CONFIRMED
    ):
        return Certainty.CONFIRMED
    return certainty
