"""Screen each date observation for the date resolver (#396).

Every observation with a date claim is screened on its own: it either
reaches a rule of the ruleset or gets one stable rejection code. Screening
never looks at another observation's value; the only set-wide input is
the file's camera make and model (for documented clock resets).

Sidecar values are hints: evidence schema version 1 records no verified,
unique association between a sidecar and this file (ADR 0003 leaves that
to #402), so no sidecar date is rated.

Which rule reads a field is decided in ``_date_fields``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from winnow.media._date_fields import field_kind
from winnow.media._date_policy import (
    DatePolicy,
    DateRule,
    Reason,
)
from winnow.media._date_spans import is_future
from winnow.models.evidence import (
    Claim,
    EvidenceSet,
    Observation,
    ParseStatus,
    SourceKind,
    TimeValue,
)

DATE_CLAIMS: Final[frozenset[Claim]] = frozenset(
    {
        Claim.CAPTURE_TIME,
        Claim.DIGITIZED_TIME,
        Claim.MODIFIED_TIME,
        Claim.RELEASE_TIME,
        Claim.RECORDING_TIME,
        Claim.CONTAINER_CREATED_TIME,
        Claim.FILE_CREATED_TIME,
        Claim.FILE_MODIFIED_TIME,
        Claim.FILE_CHANGED_TIME,
        Claim.RECEIVED_TIME,
        Claim.UPLOADED_TIME,
    },
)
"""Claims that are dates; every observation with one gets an outcome."""

_SIDECARS: Final[frozenset[SourceKind]] = frozenset(
    {SourceKind.SIDECAR_XMP, SourceKind.SIDECAR_AAE, SourceKind.SIDECAR_TAKEOUT},
)

_ZERO_INTEGER: Final[str] = "0"
"""The raw text of a QuickTime creation field read directly as the integer
0. A demuxed date string never equals it, so FFmpeg's epoch heuristic cannot
produce it."""

_EMPTY_STATUSES: Final[frozenset[ParseStatus]] = frozenset(
    {ParseStatus.EMPTY, ParseStatus.PLACEHOLDER},
)


@dataclass(frozen=True, slots=True)
class Screened:
    """One date observation after screening.

    Args:
        index: Its index in the evidence set.
        observation: The observation.
        rule: The rule it is eligible for, or ``None`` when rejected.
        reason: Why it was rejected, or ``None`` when eligible.
        value: Its parsed time value, when it has one.
    """

    index: int
    observation: Observation
    rule: DateRule | None
    reason: Reason | None
    value: TimeValue | None


@dataclass(frozen=True, slots=True)
class _Context:
    """Set-wide inputs to screening.

    Args:
        policy: The resolver policy.
        target: The claim being decided.
        rules: The rules for that claim, in firing order.
        file_path: The file the evidence is about.
        makes: The file's camera make texts.
        models: The file's camera model texts.
    """

    policy: DatePolicy
    target: Claim
    rules: tuple[DateRule, ...]
    file_path: Path | None
    makes: frozenset[str]
    models: frozenset[str]


def screen(
    evidence: EvidenceSet,
    *,
    policy: DatePolicy,
    target: Claim,
    rules: tuple[DateRule, ...],
) -> tuple[Screened, ...]:
    """Screen every date observation in ``evidence`` for one claim.

    Args:
        evidence: The file's evidence.
        policy: The resolver policy.
        target: The claim being decided (capture or digitized).
        rules: The rules for that claim, in firing order.

    Returns:
        One entry per observation with a date claim, in evidence order.
    """
    context = _Context(
        policy=policy,
        target=target,
        rules=rules,
        file_path=None if evidence.snapshot is None else evidence.snapshot.path,
        makes=_device_texts(evidence, field="make"),
        models=_device_texts(evidence, field="model"),
    )
    return tuple(
        _screen_one(index, observation, context=context)
        for index, observation in enumerate(evidence.observations)
        if observation.claim in DATE_CLAIMS
    )


def _screen_one(index: int, observation: Observation, *, context: _Context) -> Screened:
    """Screen one date observation.

    Args:
        index: Its index in the evidence set.
        observation: The observation.
        context: Set-wide inputs.

    Returns:
        The screened entry.
    """
    value = observation.value if isinstance(observation.value, TimeValue) else None
    reason = _claim_reason(observation, context=context)
    if reason is None and value is None:
        reason = _status_reason(observation)
    if reason is not None or value is None:
        return Screened(
            index=index,
            observation=observation,
            rule=None,
            reason=reason,
            value=value,
        )
    rule: DateRule | None = None
    for check in _CHECKS:
        found = check(observation, value, context)
        if isinstance(found, Reason):
            reason = found
            break
        rule = found or rule
    return Screened(
        index=index,
        observation=observation,
        rule=rule if reason is None else None,
        reason=reason,
        value=value,
    )


def _claim_reason(observation: Observation, *, context: _Context) -> Reason | None:
    """Reject an observation of another claim, and unset container times.

    Args:
        observation: The observation.
        context: Set-wide inputs.

    Returns:
        ``zero-container-time`` for a QuickTime creation field read as
        integer 0, the not-this-claim code for any other claim, the status code for
        an unparsed value, otherwise ``None``.
    """
    if (
        observation.claim is Claim.CONTAINER_CREATED_TIME
        and observation.source is SourceKind.QUICKTIME
        and observation.raw == _ZERO_INTEGER
    ):
        return Reason.ZERO_CONTAINER_TIME
    if observation.claim is not context.target:
        if context.target is Claim.CAPTURE_TIME:
            return Reason.NOT_CAPTURE
        return Reason.NOT_DIGITIZED
    if observation.status is not ParseStatus.OK:
        return _status_reason(observation)
    return None


def _status_reason(observation: Observation) -> Reason:
    """Name why an observation has no usable value.

    Args:
        observation: The observation.

    Returns:
        ``placeholder`` for an empty or placeholder value, otherwise
        ``unparsed``.
    """
    if observation.status in _EMPTY_STATUSES:
        return Reason.PLACEHOLDER
    return Reason.UNPARSED


def _check_origin(
    observation: Observation,
    value: TimeValue,
    context: _Context,
) -> Reason | None:
    """Check that the value comes from the file itself.

    Args:
        observation: The observation.
        value: Its value (unused; every check has the same signature).
        context: Set-wide inputs.

    Returns:
        ``sidecar-unverified`` for any sidecar value (no verified unique
        association exists in evidence schema version 1), ``foreign-origin``
        for an embedded field read from another file, otherwise ``None``.
    """
    del value
    if observation.source in _SIDECARS:
        return Reason.SIDECAR_UNVERIFIED
    if observation.origin != context.file_path:
        return Reason.FOREIGN_ORIGIN
    return None


def _check_rule(
    observation: Observation,
    value: TimeValue,
    context: _Context,
) -> DateRule | Reason:
    """Find the rule that reads this field with this kind of offset.

    Args:
        observation: The observation.
        value: Its value.
        context: Set-wide inputs.

    Returns:
        The rule, ``no-rule`` when no rule reads the field, or
        ``offset-not-explicit`` when a rule reads it but not with this
        offset origin.
    """
    kind = field_kind(observation)
    candidates = [rule for rule in context.rules if rule.kind is kind]
    if not candidates:
        return Reason.NO_RULE
    for rule in candidates:
        if value.offset_origin in rule.offsets:
            return rule
    return Reason.OFFSET_NOT_EXPLICIT


def _check_reset(
    observation: Observation,
    value: TimeValue,
    context: _Context,
) -> Reason | None:
    """Reject a documented camera reset value.

    Args:
        observation: The observation.
        value: Its value (unused).
        context: Set-wide inputs.

    Returns:
        ``placeholder`` when a known clock default for this file's make and
        model names this exact locator and raw text, otherwise ``None``.
    """
    del value
    for default in context.policy.known_clock_defaults:
        if (
            default.locator == observation.locator
            and default.raw == observation.raw
            and default.make in context.makes
            and default.model in context.models
        ):
            return Reason.PLACEHOLDER
    return None


def _check_future(
    observation: Observation,
    value: TimeValue,
    context: _Context,
) -> Reason | None:
    """Reject a value later than the policy's ``now``.

    Args:
        observation: The observation (unused).
        value: Its value.
        context: Set-wide inputs.

    Returns:
        ``future`` or ``None``.
    """
    del observation
    if is_future(value, now_utc=context.policy.now_utc):
        return Reason.FUTURE
    return None


def _check_filename(
    observation: Observation,
    value: TimeValue,
    context: _Context,
) -> Reason | None:
    """Keep a file-name date as a hint when the policy disallows it.

    Args:
        observation: The observation.
        value: Its value (unused).
        context: Set-wide inputs.

    Returns:
        ``filename-disabled`` or ``None``.
    """
    del value
    if observation.source is SourceKind.FILENAME and not (
        context.policy.allow_filename_dates
    ):
        return Reason.FILENAME_DISABLED
    return None


_Check = Callable[[Observation, TimeValue, _Context], DateRule | Reason | None]

_CHECKS: Final[tuple[_Check, ...]] = (
    _check_origin,
    _check_rule,
    _check_reset,
    _check_future,
    _check_filename,
)
"""Screening checks in order; the first rejection wins."""


def _device_texts(evidence: EvidenceSet, *, field: str) -> frozenset[str]:
    """Return the file's camera make or model texts.

    Args:
        evidence: The file's evidence.
        field: ``"make"`` or ``"model"``; matched in the locator, so a make
            written in the model field never selects a reset profile.

    Returns:
        The stripped text of every parsed ``device`` observation whose
        locator names that field.
    """
    names: set[str] = set()
    for observation in evidence.observations:
        if observation.claim is not Claim.DEVICE:
            continue
        if observation.status is not ParseStatus.OK:
            continue
        if field not in observation.locator.lower():
            continue
        text = observation.value if isinstance(observation.value, str) else None
        text = text if text is not None else observation.raw
        if text is not None:
            names.add(text.strip(" \x00"))
    return frozenset(names)
