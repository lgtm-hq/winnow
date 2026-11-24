"""Select, compare and grade for the date resolver (#396).

Steps 2 to 4 of :mod:`winnow.media.resolve_date`: find the first rule that
fires and its single value, place every other date observation relative to
that value, and grade the result.

Two values of one property (an original and its copy, IPTC and XMP
``DateCreated``) are compared by the clock readings they write when only one
of them carries an offset: that links two representations of one field and
gives neither a zone. Any other wall clock and instant are incomparable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import combinations
from typing import Final

from winnow.media._date_policy import (
    BLOCKS_CONFIRMED,
    KNOWN_EDITORS,
    DatePolicy,
    DateRule,
    FieldKind,
    Reason,
    WarningCode,
)
from winnow.media._date_screen import Screened
from winnow.media._date_spans import (
    Agreement,
    compare,
    fineness,
    is_suspicious_default,
    same_instant,
    span_of,
    written_dates_differ,
    written_span,
)
from winnow.models.evidence import (
    Certainty,
    Claim,
    EvidenceSet,
    ParseStatus,
    Precision,
    SourceKind,
    TimeValue,
)

_COARSER_THAN_DAY: Final[frozenset[Precision]] = frozenset(
    {Precision.YEAR, Precision.MONTH},
)

_LINKED_KINDS: Final[frozenset[FieldKind]] = frozenset(
    {FieldKind.ORIGINAL, FieldKind.DATE_CREATED},
)
"""Field kinds whose observations are representations of one property."""

_SOURCE_PREFERENCE: Final[dict[SourceKind, int]] = {
    SourceKind.EXIF: 0,
    SourceKind.QUICKTIME: 1,
    SourceKind.IPTC: 2,
    SourceKind.PNG_TEXT: 3,
    SourceKind.XMP: 4,
    SourceKind.FILENAME: 5,
}
"""Which representation of one value to keep: the camera-written block
before copies, so an XMP copy in UTC never replaces the EXIF wall time."""

_UNPREFERRED: Final[int] = 99

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


def relate(
    first: TimeValue,
    first_kind: FieldKind,
    second: TimeValue,
    second_kind: FieldKind,
    *,
    tolerance: timedelta,
) -> Agreement:
    """Compare two values, linking two representations of one property.

    Args:
        first: One value.
        first_kind: Its field kind.
        second: The other value.
        second_kind: Its field kind.
        tolerance: Extra gap allowed; zero requires overlap.

    Returns:
        The agreement of their spans. When exactly one has an offset and
        both are the same linked property, their written readings are
        compared instead; otherwise such a pair is ``INCOMPARABLE``.
    """
    first_span, second_span = span_of(first), span_of(second)
    linked = first_kind is second_kind and first_kind in _LINKED_KINDS
    if first_span.is_instant != second_span.is_instant and linked:
        first_span, second_span = written_span(first), written_span(second)
    return compare(first_span, second_span, tolerance=tolerance)


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


def _preference(entry: Screened) -> tuple[bool, tuple[int, int], int]:
    """Rank one candidate's representation of the firing rule's value.

    Args:
        entry: A candidate of the firing rule.

    Returns:
        A key that is larger for a value with an offset, then for a finer
        value, then for the camera-written block over a copy.
    """
    value = value_of(entry)
    source = _SOURCE_PREFERENCE.get(entry.observation.source, _UNPREFERRED)
    return value.offset_minutes is not None, fineness(value), -source


def single_value(top: Sequence[Screened]) -> TimeValue | None:
    """Return the one value the firing rule's candidates give.

    Every pair must overlap exactly (no tolerance joins two candidates of one
    rule). The preferred representation is kept; any other candidate that is
    not the same instant must write the same calendar date, so a finer copy
    in another zone can never move the date.

    Args:
        top: The firing rule's entries.

    Returns:
        The chosen value, or ``None`` when the candidates contradict each
        other, cannot be compared, or leave more than one preferred value;
        a tie is never broken by input order or by the timestamps.
    """
    kind = rule_of(top[0]).kind
    values = [value_of(entry) for entry in top]
    for first, second in combinations(values, 2):
        if relate(first, kind, second, kind, tolerance=timedelta(0)) is not (
            Agreement.AGREE
        ):
            return None
    best = max(_preference(entry) for entry in top)
    distinct: list[TimeValue] = []
    for entry in top:
        value = value_of(entry)
        if _preference(entry) == best and value not in distinct:
            distinct.append(value)
    if len(distinct) != 1:
        return None
    chosen = distinct[0]
    for value in values:
        if value == chosen or same_instant(value, chosen):
            continue
        if written_dates_differ(value, chosen):
            return None
    return chosen


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
    kind = rule_of(top[0]).kind
    families = frozenset(entry.observation.family for entry in top)
    outcome = Outcome(chosen=list(top))
    for entry in screened:
        if entry in top:
            continue
        if entry.rule is None:
            outcome.rejected.append(
                (entry, _rejection_reason(entry, value, policy, claim)),
            )
            continue
        agreement = relate(
            value,
            kind,
            value_of(entry),
            entry.rule.kind,
            tolerance=(
                policy.gps_tolerance
                if entry.rule.kind is FieldKind.GPS
                else policy.agreement_tolerance
            ),
        )
        _place(entry, agreement=agreement, families=families, outcome=outcome)
    return outcome


def _place(
    entry: Screened,
    *,
    agreement: Agreement,
    families: frozenset[str],
    outcome: Outcome,
) -> None:
    """Place an eligible entry of a lower rule.

    Args:
        entry: The entry.
        agreement: How it relates to the chosen value.
        families: Provenance families of the firing rule's entries; a GPS
            fix in one of them is not an independent witness.
        outcome: Updated in place.
    """
    kind = rule_of(entry).kind
    if agreement is Agreement.AGREE:
        outcome.chosen.append(entry)
        independent = entry.observation.family not in families
        outcome.gps_agrees |= kind is FieldKind.GPS and independent
    elif agreement is Agreement.INCOMPARABLE:
        outcome.incomparable.append(entry)
    elif kind is FieldKind.GPS:
        outcome.rejected.append((entry, Reason.GPS_DISAGREES))
    elif kind is FieldKind.FILENAME:
        outcome.rejected.append((entry, Reason.FILENAME_DIFFERS))
    else:
        outcome.conflicts.append(entry)


def _rejection_reason(
    entry: Screened,
    value: TimeValue,
    policy: DatePolicy,
    claim: Claim,
) -> Reason:
    """Name a rejected entry's outcome, comparing container creation.

    Args:
        entry: A rejected entry.
        value: The chosen value.
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
            span_of(value),
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
    if _names_an_editor(evidence):
        found.add(WarningCode.EDITING_SOFTWARE)
    if evidence.issues:
        found.add(WarningCode.READ_ISSUES)
    return frozenset(found)


def _names_an_editor(evidence: EvidenceSet) -> bool:
    """Tell whether a ``software`` observation names a known editor.

    Args:
        evidence: The file's evidence.

    Returns:
        Whether a parsed ``software`` text starts with a
        :data:`KNOWN_EDITORS` prefix, ignoring case.
    """
    for observation in evidence.observations:
        if observation.claim is not Claim.SOFTWARE:
            continue
        if observation.status is not ParseStatus.OK:
            continue
        text = observation.value if isinstance(observation.value, str) else None
        text = (text if text is not None else observation.raw) or ""
        if text.strip(" \x00").casefold().startswith(KNOWN_EDITORS):
            return True
    return False


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
        NONE when a reset-like value is contradicted; otherwise the rule's
        certainty (WEAK for a coarse value where the rule says so), one
        level lower with a conflict, capped at WEAK for a reset-like value,
        and CONFIRMED only when :func:`_confirms` holds.
    """
    certainty = rule.certainty
    if rule.weak_when_coarse and value.precision in _COARSER_THAN_DAY:
        certainty = Certainty.WEAK
    suspicious = WarningCode.SUSPICIOUS_CLOCK_DEFAULT in warnings
    if outcome.conflicts:
        certainty = Certainty.NONE if suspicious else _LOWER[certainty]
    if certainty is Certainty.NONE:
        return certainty
    if suspicious:
        return Certainty.WEAK
    if _confirms(rule, certainty=certainty, outcome=outcome, warnings=warnings):
        return Certainty.CONFIRMED
    return certainty


def _confirms(
    rule: DateRule,
    *,
    certainty: Certainty,
    outcome: Outcome,
    warnings: frozenset[WarningCode],
) -> bool:
    """Tell whether a PROBABLE result is CONFIRMED.

    Args:
        rule: The firing rule.
        certainty: The certainty so far.
        outcome: Where every entry ended up.
        warnings: The decision's warnings.

    Returns:
        Whether the rule is confirmable, an independent GPS fix agrees,
        nothing conflicts, no capture field other than a file name is
        incomparable, and no warning in :data:`BLOCKS_CONFIRMED` applies.
    """
    blocking_incomparable = any(
        rule_of(entry).kind is not FieldKind.FILENAME for entry in outcome.incomparable
    )
    return (
        rule.confirmable
        and certainty is Certainty.PROBABLE
        and outcome.gps_agrees
        and not outcome.conflicts
        and not blocking_incomparable
        and not warnings & BLOCKS_CONFIRMED
    )
