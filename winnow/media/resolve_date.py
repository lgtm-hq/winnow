"""Decide a file's capture date from its evidence (#396).

:func:`resolve_capture_date` is a pure function of an
:class:`~winnow.models.evidence.EvidenceSet` and a :class:`DatePolicy`. It
reads no file, no clock and no host timezone. It returns a
:class:`~winnow.models.evidence.Decision` naming the rule that chose, every
observation it used, every one it set aside and why, and how certain the
result is. When the evidence does not support an answer the result is
``undated`` (or ``conflicting-capture``), never a guess.

Steps (ADR 0004, "Capture-date resolver"); screening is in ``_date_screen``,
selection and grading in ``_date_select``:

1. **Screen** every date observation on its own: edit,
   copy, arrival, release, digitizing, recording, container and filesystem
   times are ``not-capture``; unparsed, placeholder, documented reset,
   future and unset container values are rejected with their code.
2. **Select** the first rule in :data:`CAPTURE_RULES` with an eligible
   observation. ``gps-only`` fires only when GPS is all there is. Every
   candidate of that rule must agree at its stated precision and give one
   finest value, otherwise the result is ``conflicting-capture``: ties are
   never broken by input order or by the timestamps themselves.
3. **Compare** every other eligible observation with the chosen value.
   Agreeing ones join ``chosen``; a wall clock and an instant are
   ``incomparable``; a disagreeing one is a conflict and lowers certainty
   by one level (a GPS fix that disagrees is ``gps-disagrees`` instead,
   because a fix need not be contemporaneous with the shutter). Container
   creation is compared as a hint only.
4. **Certainty.** A lone timestamp is at most PROBABLE. CONFIRMED needs an
   explicit-offset rule, an agreeing GPS fix, no conflict, nothing
   incomparable and no warning in :data:`BLOCKS_CONFIRMED`.

Wall clocks stay naive with no offset: no zone is derived from GPS,
siblings, rounding or the host.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from winnow.media._date_policy import (
    BLOCKS_CONFIRMED,
    CAPTURE_RULES,
    CONFLICTING_CAPTURE,
    CONFLICTING_DIGITIZED,
    DIGITIZED_RULES,
    RULESET_VERSION,
    UNDATED,
    ClockDefault,
    DatePolicy,
    DateRule,
    Reason,
    WarningCode,
)
from winnow.media._date_screen import Screened, screen
from winnow.media._date_select import (
    compare_rest,
    grade,
    rule_of,
    single_value,
    top_candidates,
    warnings_for,
)
from winnow.models.evidence import (
    Certainty,
    Claim,
    Decision,
    EvidenceSet,
    Rejection,
)

_UNRANKED: Final[int] = 1_000
"""Sort rank of an observation no rule reads."""


def resolve_capture_date(evidence: EvidenceSet, *, policy: DatePolicy) -> Decision:
    """Decide when the picture or recording in a file was taken.

    Args:
        evidence: The file's evidence.
        policy: The clock and options; nothing is read from the host.

    Returns:
        A ``capture_time`` decision. Its value is a
        :class:`~winnow.models.evidence.TimeValue` exactly as one chosen
        observation states it, or ``None`` when the file is undated or its
        evidence conflicts.
    """
    return _resolve(
        evidence,
        policy=policy,
        claim=Claim.CAPTURE_TIME,
        rules=CAPTURE_RULES,
        conflict_rule=CONFLICTING_CAPTURE,
    )


def resolve_digitized_date(evidence: EvidenceSet, *, policy: DatePolicy) -> Decision:
    """Decide when a file was digitized, from digitized fields only.

    The result never fills the capture date: a scan with only a scan time
    has an undated capture and a dated digitization.

    Args:
        evidence: The file's evidence.
        policy: The clock and options; nothing is read from the host.

    Returns:
        A ``digitized_time`` decision.
    """
    return _resolve(
        evidence,
        policy=policy,
        claim=Claim.DIGITIZED_TIME,
        rules=DIGITIZED_RULES,
        conflict_rule=CONFLICTING_DIGITIZED,
    )


def _resolve(
    evidence: EvidenceSet,
    *,
    policy: DatePolicy,
    claim: Claim,
    rules: tuple[DateRule, ...],
    conflict_rule: str,
) -> Decision:
    """Run the four steps for one claim.

    Args:
        evidence: The file's evidence.
        policy: The resolver policy.
        claim: The claim decided.
        rules: Its rules, in firing order.
        conflict_rule: Rule id when its evidence contradicts itself.

    Returns:
        The decision.
    """
    screened = screen(evidence, policy=policy, target=claim, rules=rules)
    top = top_candidates(screened)
    if not top:
        return _without_value(evidence, screened, claim=claim, rule_id=UNDATED)
    value = single_value(top, policy=policy)
    if value is None:
        return _without_value(
            evidence,
            screened,
            claim=claim,
            rule_id=conflict_rule,
            conflicts=top,
        )
    outcome = compare_rest(screened, top=top, value=value, policy=policy, claim=claim)
    rule = rule_of(top[0])
    warnings = warnings_for(evidence, value=value, top=top)
    certainty = grade(rule, value=value, outcome=outcome, warnings=warnings)
    if certainty is Certainty.NONE:
        return _without_value(
            evidence,
            screened,
            claim=claim,
            rule_id=conflict_rule,
            conflicts=[*top, *outcome.conflicts],
        )
    return Decision(
        claim=claim,
        value=value,
        certainty=certainty,
        rule_id=rule.rule_id,
        rule_version=RULESET_VERSION,
        chosen=_indices(outcome.chosen),
        rejected=_rejections(outcome.rejected),
        conflicts=_indices(outcome.conflicts),
        warnings=tuple(code.value for code in WarningCode if code in warnings),
        incomparable=_indices(outcome.incomparable),
    )


def _without_value(
    evidence: EvidenceSet,
    screened: Sequence[Screened],
    *,
    claim: Claim,
    rule_id: str,
    conflicts: Sequence[Screened] = (),
) -> Decision:
    """Build an undated or conflicting decision.

    Args:
        evidence: The file's evidence.
        screened: Every screened entry.
        claim: The claim decided.
        rule_id: ``undated`` or the claim's conflict rule.
        conflicts: The contradicting candidates.

    Returns:
        A decision with no value and certainty NONE; every eligible entry
        not in conflict is ``lower-rank``.
    """
    rejected = [
        (entry, entry.reason or Reason.LOWER_RANK)
        for entry in screened
        if entry not in conflicts
    ]
    warnings = (WarningCode.READ_ISSUES.value,) if evidence.issues else ()
    return Decision(
        claim=claim,
        value=None,
        certainty=Certainty.NONE,
        rule_id=rule_id,
        rule_version=RULESET_VERSION,
        chosen=(),
        rejected=_rejections(rejected),
        conflicts=_indices(conflicts),
        warnings=warnings,
    )


def _sort_key(entry: Screened) -> tuple[int, str, int]:
    """Order entries by rule rank, then observation identity.

    Args:
        entry: A screened entry.

    Returns:
        The key; input order only separates identical observations.
    """
    order = _UNRANKED if entry.rule is None else entry.rule.order
    return order, entry.observation.id, entry.index


def _indices(entries: Iterable[Screened]) -> tuple[int, ...]:
    """Return entries' indices in canonical order.

    Args:
        entries: Screened entries.

    Returns:
        Their observation indices.
    """
    return tuple(entry.index for entry in sorted(entries, key=_sort_key))


def _rejections(entries: Iterable[tuple[Screened, Reason]]) -> tuple[Rejection, ...]:
    """Return rejections in canonical order.

    Args:
        entries: Rejected entries with their codes.

    Returns:
        The rejections.
    """
    ordered = sorted(entries, key=lambda pair: _sort_key(pair[0]))
    return tuple(
        Rejection(observation=entry.index, reason=reason.value)
        for entry, reason in ordered
    )


__all__ = [
    "BLOCKS_CONFIRMED",
    "CAPTURE_RULES",
    "CONFLICTING_CAPTURE",
    "CONFLICTING_DIGITIZED",
    "DIGITIZED_RULES",
    "RULESET_VERSION",
    "UNDATED",
    "ClockDefault",
    "DatePolicy",
    "DateRule",
    "Reason",
    "WarningCode",
    "resolve_capture_date",
    "resolve_digitized_date",
]
