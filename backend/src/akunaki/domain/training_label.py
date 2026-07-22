"""Deterministic training label (``training_label_v0.1.0``).

Pure: no I/O, no clock. These are the exact v0.1.0 rules from health-engine.md.

The label is the base band from the recovery score, then an **ordered
downshift**: each step can only lower the label, never raise it. Two invariants
the design stresses:

- **Missing data never produces ``rest``.** A null/insufficient recovery yields
  ``insufficient`` (and reconnect guidance elsewhere), not rest.
- A high-severity anomaly alone floors the label at ``light`` — never rest.

Rest comes only from a low recovery score (< 40) or an explicit severe symptom
flag on a completed check-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

RULESET_VERSION = "training_label_v0.1.0"

# Recovery-band edges (exact v0.1.0).
_REST_MAX = 40  # score < 40 -> rest
_LIGHT_MAX = 54  # 40..54 -> light
_MODERATE_MAX = 74  # 55..74 -> moderate; >= 75 -> hard candidate

# Gates for the top band and downshift.
_HARD_MIN_CONFIDENCE = 0.7
_INSUFFICIENT_CONFIDENCE = 0.4
_HIGH_SYMPTOM_BURDEN = 0.75
_ACWR_RED = 1.3
_HRV_COMPONENT_LOW = 40.0


class TrainingLabel(StrEnum):
    """The five possible training labels."""

    INSUFFICIENT = "insufficient"
    REST = "rest"
    LIGHT = "light"
    MODERATE = "moderate"
    HARD = "hard"


# Ordering for "at most" downshifts: lower rank cannot be raised.
class _Rank(IntEnum):
    INSUFFICIENT = 0
    REST = 1
    LIGHT = 2
    MODERATE = 3
    HARD = 4


_LABEL_TO_RANK = {
    TrainingLabel.INSUFFICIENT: _Rank.INSUFFICIENT,
    TrainingLabel.REST: _Rank.REST,
    TrainingLabel.LIGHT: _Rank.LIGHT,
    TrainingLabel.MODERATE: _Rank.MODERATE,
    TrainingLabel.HARD: _Rank.HARD,
}
_RANK_TO_LABEL = {rank: label for label, rank in _LABEL_TO_RANK.items()}


@dataclass(frozen=True, slots=True)
class TrainingInputs:
    """Everything the label rules read.

    ``recovery_score`` is None when insufficient. ``acwr`` is None when
    undefined. ``hrv_component_c`` is the HRV component's 0-100 score, or None
    when absent.
    """

    recovery_score: int | None
    recovery_status: str
    confidence: float
    has_high_severity_anomaly: bool
    symptom_burden_n: float | None
    severe_symptom_flag: bool
    acwr: float | None
    hrv_component_c: float | None


@dataclass(frozen=True, slots=True)
class TrainingResult:
    """A computed training label."""

    label: TrainingLabel
    ruleset_version: str = RULESET_VERSION


def training_label(inputs: TrainingInputs) -> TrainingResult:
    """Compute the training label: base band then ordered downshift."""
    base = _base_label(inputs)
    label = base

    # 1. Insufficient/rest are terminal for the downshift path.
    if label in (TrainingLabel.INSUFFICIENT, TrainingLabel.REST):
        return TrainingResult(label=label)

    high_symptom = (
        inputs.symptom_burden_n is not None and inputs.symptom_burden_n >= _HIGH_SYMPTOM_BURDEN
    ) or inputs.severe_symptom_flag

    # 2. Active high-severity anomaly floors at light (never rest).
    if inputs.has_high_severity_anomaly:
        label = _at_most(label, TrainingLabel.LIGHT)

    # 3. High symptom burden floors at light.
    if high_symptom:
        label = _at_most(label, TrainingLabel.LIGHT)

    # 4. A hard base with low confidence or non-ok status caps at moderate.
    if base is TrainingLabel.HARD and (
        inputs.confidence < _HARD_MIN_CONFIDENCE or inputs.recovery_status != "ok"
    ):
        label = _at_most(label, TrainingLabel.MODERATE)

    # 5. ACWR red band caps hard at moderate; with a weak HRV component, light.
    if inputs.acwr is not None and inputs.acwr > _ACWR_RED and label is TrainingLabel.HARD:
        label = _at_most(label, TrainingLabel.MODERATE)
        if inputs.hrv_component_c is not None and inputs.hrv_component_c < _HRV_COMPONENT_LOW:
            label = _at_most(label, TrainingLabel.LIGHT)

    return TrainingResult(label=label)


def _base_label(inputs: TrainingInputs) -> TrainingLabel:
    """The base label from the recovery-score bands (before downshift)."""
    if (
        inputs.recovery_score is None
        or inputs.recovery_status == "insufficient"
        or inputs.confidence < _INSUFFICIENT_CONFIDENCE
    ):
        return TrainingLabel.INSUFFICIENT

    score = inputs.recovery_score
    # Rest also comes from an explicit severe symptom flag.
    if score < _REST_MAX or inputs.severe_symptom_flag:
        return TrainingLabel.REST
    if score <= _LIGHT_MAX:
        return TrainingLabel.LIGHT
    if score <= _MODERATE_MAX:
        return TrainingLabel.MODERATE

    # Hard requires the full gate; otherwise the score falls to moderate.
    if (
        inputs.recovery_status == "ok"
        and inputs.confidence >= _HARD_MIN_CONFIDENCE
        and not inputs.has_high_severity_anomaly
        and not _has_high_symptom(inputs)
        and not (inputs.acwr is not None and inputs.acwr > _ACWR_RED)
    ):
        return TrainingLabel.HARD
    return TrainingLabel.MODERATE


def _has_high_symptom(inputs: TrainingInputs) -> bool:
    return (
        inputs.symptom_burden_n is not None and inputs.symptom_burden_n >= _HIGH_SYMPTOM_BURDEN
    ) or inputs.severe_symptom_flag


def _at_most(current: TrainingLabel, ceiling: TrainingLabel) -> TrainingLabel:
    """Lower ``current`` to ``ceiling`` when it is higher; never raise."""
    return _RANK_TO_LABEL[min(_LABEL_TO_RANK[current], _LABEL_TO_RANK[ceiling])]
