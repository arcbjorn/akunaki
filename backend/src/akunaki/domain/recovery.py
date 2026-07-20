"""Deterministic general-recovery composite (``general_recovery_v0.1.0``).

Pure: no I/O, no clock. ``as_of_at`` and every input timestamp are supplied by
the caller. These are the exact v0.1.0 formulas from health-engine.md.

The composite is assembled from **present** components. A component that is
absent (missing input, insufficient baseline, undefined ACWR, no completed
check-in) is omitted from the weight set entirely — it never contributes an
invented midpoint. Coverage is always disclosed, so the caller can show which
optional components were missing rather than hiding it.

This module intentionally takes each component's mapped 0-100 score ``c`` as an
input. Turning raw signals into ``c`` (rolling baselines, ACWR, check-in
normalization) happens upstream; the two exact mappings that are pure functions
of a single number — the baseline z-score curve and sleep-target adherence —
live here as helpers so they are golden-tested in one place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

FORMULA_VERSION = "general_recovery_v0.1.0"

# Provisional default sleep target when the user has set none. Explicitly
# provisional per the design, never a chronically short personal median.
DEFAULT_SLEEP_TARGET_MIN = 480

# Sufficiency gate: minimum share of the full weight set that must be present.
MIN_AVAILABLE_WEIGHT = 0.60

# Baseline maturity multipliers for the confidence factor.
_MATURITY_MIN = 0.85
_MATURITY_MATURE = 1.0

# Quality weights for the confidence factor.
_QUALITY_WEIGHTS = {"high": 1.0, "medium": 0.75, "low": 0.5, "unknown": 0.5}


class RecoveryStatus(StrEnum):
    """Outcome of a recovery evaluation."""

    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class ComponentCode(StrEnum):
    """The recovery composite's contributing components."""

    HRV = "hrv"
    RESTING_HR = "resting_hr"
    SLEEP_ADHERENCE = "sleep_adherence"
    SLEEP_EFFICIENCY = "sleep_efficiency"
    SLEEP_CONSISTENCY = "sleep_consistency"
    TEMPERATURE = "temperature"
    RESPIRATORY = "respiratory"
    PRIOR_LOAD_BALANCE = "prior_load_balance"
    SUBJECTIVE = "subjective"


# Exact v0.1.0 weights; sum to 1.00 when every component is present.
COMPONENT_WEIGHTS: dict[ComponentCode, float] = {
    ComponentCode.HRV: 0.25,
    ComponentCode.RESTING_HR: 0.15,
    ComponentCode.SLEEP_ADHERENCE: 0.20,
    ComponentCode.SLEEP_EFFICIENCY: 0.05,
    ComponentCode.SLEEP_CONSISTENCY: 0.05,
    ComponentCode.TEMPERATURE: 0.10,
    ComponentCode.RESPIRATORY: 0.05,
    ComponentCode.PRIOR_LOAD_BALANCE: 0.10,
    ComponentCode.SUBJECTIVE: 0.05,
}

# The sleep component group (gate 1 requires at least sleep-target adherence).
_SLEEP_GROUP = {
    ComponentCode.SLEEP_ADHERENCE,
    ComponentCode.SLEEP_EFFICIENCY,
    ComponentCode.SLEEP_CONSISTENCY,
}

# Critical inputs for the freshness minimum and quality mean.
_CRITICAL = {ComponentCode.SLEEP_ADHERENCE, ComponentCode.HRV, ComponentCode.RESTING_HR}


class BaselineMaturity(StrEnum):
    """Maturity of a component's rolling baseline."""

    MIN = "min"
    MATURE = "mature"


@dataclass(frozen=True, slots=True)
class RecoveryComponent:
    """One present component's contribution to the composite.

    ``c`` is the already-mapped 0-100 component score. ``freshness_hours`` is
    the age of the underlying signal relative to ``as_of_at`` (only used for
    critical inputs). ``quality`` and ``baseline_maturity`` feed confidence;
    ``baseline_maturity`` is None for components that use no baseline.
    """

    code: ComponentCode
    c: float
    quality: str = "unknown"
    freshness_hours: float | None = None
    baseline_maturity: BaselineMaturity | None = None


@dataclass(frozen=True, slots=True)
class RecoveryFactor:
    """A signed contributor to the derivation, for disclosure."""

    factor_code: str
    present: bool
    weight: float
    magnitude: float
    """The component's 0-100 score; 0 when absent."""


@dataclass(frozen=True, slots=True)
class RecoveryGap:
    """A disclosed reason the recovery evaluation is incomplete."""

    code: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """A recovery evaluation. ``score`` is None when the gate fails."""

    status: RecoveryStatus
    score: int | None
    available_weight: float
    confidence: float
    factors: tuple[RecoveryFactor, ...]
    formula_version: str = FORMULA_VERSION


def baseline_component_score(z: float, *, direction: float = 1.0) -> float:
    """Map a baseline z-score to a 0-100 component score (exact v0.1.0).

    ``direction`` applies the component's directed mapping before the curve:
    ``+1`` for better-when-higher, ``-1`` for better-when-lower. The directed
    z is clamped to [-3, 3], then ``c = clamp(50 + 50*tanh(z_dir/2), 0, 100)``.
    """
    z_dir = _clamp(direction * z, -3.0, 3.0)
    return _clamp(50.0 + 50.0 * math.tanh(z_dir / 2.0), 0.0, 100.0)


def sleep_target_adherence(*, duration_min: float, target_min: int) -> float:
    """Bounded 0-100 sleep-target adherence (exact v0.1.0).

    Shared shape with the sleep summary surface: oversleep earns no bonus.
    """
    if target_min <= 0:
        msg = "target_min must be positive"
        raise ValueError(msg)
    shortfall = max(0.0, target_min - duration_min)
    return _clamp(100.0 * (1.0 - shortfall / target_min), 0.0, 100.0)


def freshness_one(hours: float) -> float:
    """Piecewise per-input freshness (exact v0.1.0), in [0, 1].

    1 through 24h; linear to 0.5 at 72h; linear to 0 at 168h; 0 beyond. A
    future timestamp (negative age) is treated as fresh.
    """
    h = max(0.0, hours)
    if h <= 24.0:
        return 1.0
    if h <= 72.0:
        return 1.0 - 0.5 * (h - 24.0) / 48.0
    if h <= 168.0:
        return 0.5 * (168.0 - h) / 96.0
    return 0.0


def evaluate_recovery(components: list[RecoveryComponent]) -> RecoveryResult:
    """Assemble the recovery composite from its present components.

    Absent components are simply not passed in. The gate, weighted mean over
    present weights, coverage, and confidence follow health-engine.md exactly.
    """
    present = {comp.code: comp for comp in components}
    if len(present) != len(components):
        msg = "duplicate component codes"
        raise ValueError(msg)

    available_weight = sum(COMPONENT_WEIGHTS[code] for code in present)
    factors = _build_factors(present)

    if not _gate_passes(present, available_weight):
        return RecoveryResult(
            status=RecoveryStatus.INSUFFICIENT,
            score=None,
            available_weight=available_weight,
            confidence=0.0,
            factors=factors,
        )

    # Weighted mean renormalized over present weights (coverage disclosed).
    weighted = sum(COMPONENT_WEIGHTS[code] * comp.c for code, comp in present.items())
    score = round(weighted / available_weight)

    confidence = _confidence(present, available_weight)
    # Low confidence or missing non-critical components is partial, not a fake
    # ok. Full weight and full confidence is the only path to ok.
    status = (
        RecoveryStatus.OK
        if available_weight >= 0.999 and confidence >= 0.999
        else RecoveryStatus.PARTIAL
    )
    return RecoveryResult(
        status=status,
        score=score,
        available_weight=available_weight,
        confidence=confidence,
        factors=factors,
    )


def recovery_data_gaps(components: list[RecoveryComponent]) -> tuple[RecoveryGap, ...]:
    """Disclosed gate shortfalls for a component set, in a stable order.

    Empty when the sufficiency gate passes. Each gap names a concrete missing
    requirement so a client can explain *why* a score is withheld rather than
    showing a fabricated midpoint.
    """
    return recovery_data_gaps_from_codes({comp.code for comp in components})


def recovery_data_gaps_from_codes(
    present: set[ComponentCode],
) -> tuple[RecoveryGap, ...]:
    """Disclosed gate shortfalls given the set of present component codes.

    The pure core shared by the live evaluation and the stored read path, so a
    score served from storage discloses exactly the same gaps it was computed
    with.
    """
    available_weight = sum(COMPONENT_WEIGHTS[code] for code in present)
    gaps: list[RecoveryGap] = []

    if ComponentCode.SLEEP_ADHERENCE not in present:
        gaps.append(RecoveryGap(code="missing_authoritative_sleep"))
    if not (ComponentCode.HRV in present or ComponentCode.RESTING_HR in present):
        gaps.append(RecoveryGap(code="missing_hrv_or_resting_hr"))
    if available_weight < MIN_AVAILABLE_WEIGHT:
        gaps.append(RecoveryGap(code="insufficient_component_coverage"))

    return tuple(gaps)


def _gate_passes(
    present: dict[ComponentCode, RecoveryComponent],
    available_weight: float,
) -> bool:
    """Recovery v0.1.0 sufficiency gate (all three must hold)."""
    sleep_group_present = bool(_SLEEP_GROUP & present.keys())
    adherence_present = ComponentCode.SLEEP_ADHERENCE in present
    hrv_or_rhr = ComponentCode.HRV in present or ComponentCode.RESTING_HR in present
    # Gate 1 names the sleep group but pins the concrete requirement to
    # sleep-target adherence being present (its authoritative sleep-duration
    # input); the group check guards against a mis-supplied component set.
    return (
        sleep_group_present
        and adherence_present
        and hrv_or_rhr
        and available_weight >= MIN_AVAILABLE_WEIGHT
    )


def _confidence(
    present: dict[ComponentCode, RecoveryComponent],
    available_weight: float,
) -> float:
    """confidence = coverage * freshness * quality * baseline_maturity."""
    c_coverage = available_weight

    critical = [comp for code, comp in present.items() if code in _CRITICAL]
    fresh_values = [
        freshness_one(comp.freshness_hours) for comp in critical if comp.freshness_hours is not None
    ]
    c_freshness = min(fresh_values) if fresh_values else 1.0

    quality_values = [_QUALITY_WEIGHTS.get(comp.quality, 0.5) for comp in critical]
    c_quality = sum(quality_values) / len(quality_values) if quality_values else 1.0

    c_baseline = _baseline_maturity_factor(present)

    return c_coverage * c_freshness * c_quality * c_baseline


def _baseline_maturity_factor(present: dict[ComponentCode, RecoveryComponent]) -> float:
    """Weighted maturity over present baseline-dependent components only."""
    weighted_sum = 0.0
    weight_total = 0.0
    for code, comp in present.items():
        if comp.baseline_maturity is None:
            continue
        multiplier = (
            _MATURITY_MATURE if comp.baseline_maturity is BaselineMaturity.MATURE else _MATURITY_MIN
        )
        weight = COMPONENT_WEIGHTS[code]
        weighted_sum += multiplier * weight
        weight_total += weight
    if weight_total == 0.0:
        return 1.0
    return weighted_sum / weight_total


def _build_factors(
    present: dict[ComponentCode, RecoveryComponent],
) -> tuple[RecoveryFactor, ...]:
    """One factor per component, present or not, for full coverage disclosure."""
    factors = []
    for code, weight in COMPONENT_WEIGHTS.items():
        comp = present.get(code)
        factors.append(
            RecoveryFactor(
                factor_code=code.value,
                present=comp is not None,
                weight=weight,
                magnitude=comp.c if comp is not None else 0.0,
            )
        )
    return tuple(factors)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
