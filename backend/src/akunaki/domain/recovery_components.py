"""Bridge from feature values to recovery components (``general_recovery_v0.1.0``).

Pure: no I/O, no clock. This is the seam between stored features and the
recovery composite. For a baseline-dependent component it computes the rolling
baseline over the prior-day series, takes the robust z-score of the current
value, applies the component's directed mapping, and maps to a 0-100 ``c`` via
the recovery curve. For a direct component (already 0-100) it passes the value
through.

The design's cardinal rule shows up here as a return type: an insufficient
baseline yields **None**, so the caller omits the component from the weight set
entirely. Nothing here ever invents a midpoint ``c`` for missing signal.
"""

from __future__ import annotations

from dataclasses import dataclass

from akunaki.domain.baseline import (
    Baseline,
    MetricFamily,
    compute_baseline,
    z_score,
)
from akunaki.domain.baseline import BaselineMaturity as StatMaturity
from akunaki.domain.recovery import BaselineMaturity as RecoveryMaturity
from akunaki.domain.recovery import (
    ComponentCode,
    Direction,
    RecoveryComponent,
    baseline_component_score,
    sleep_target_adherence,
)


@dataclass(frozen=True, slots=True)
class BaselineInput:
    """A baseline-dependent component's current value and its prior series.

    ``direction`` is the component's directed mapping (see :class:`Direction`).
    ``samples`` is the present, quality-eligible prior-day series (missing days
    already excluded).
    """

    value: float
    samples: list[float]
    family: MetricFamily
    direction: Direction
    quality: str = "unknown"
    freshness_hours: float | None = None


def map_baseline_component(
    code: ComponentCode,
    signal: BaselineInput,
) -> RecoveryComponent | None:
    """Turn a baseline-dependent signal into a component, or None to omit it.

    Returns None when the baseline is insufficient (fewer than 14 present
    samples) — the component is then absent from the composite, never a midpoint.
    """
    baseline = compute_baseline(signal.samples, family=signal.family)
    if not baseline.is_usable:
        return None
    z = z_score(signal.value, baseline)
    c = baseline_component_score(z, direction=signal.direction)
    return RecoveryComponent(
        code=code,
        c=c,
        quality=signal.quality,
        freshness_hours=signal.freshness_hours,
        baseline_maturity=_to_recovery_maturity(baseline),
    )


def map_sleep_adherence_component(
    *,
    duration_min: float,
    target_min: int,
    quality: str = "unknown",
    freshness_hours: float | None = None,
) -> RecoveryComponent:
    """The sleep-target adherence component: direct 0-100, no baseline.

    Always present when an authoritative sleep duration exists; it carries no
    baseline maturity because it uses none.
    """
    c = sleep_target_adherence(duration_min=duration_min, target_min=target_min)
    return RecoveryComponent(
        code=ComponentCode.SLEEP_ADHERENCE,
        c=c,
        quality=quality,
        freshness_hours=freshness_hours,
        baseline_maturity=None,
    )


def _to_recovery_maturity(baseline: Baseline) -> RecoveryMaturity:
    """Map a usable baseline's maturity to the recovery module's two-value enum.

    Only ``min`` and ``mature`` reach here — an insufficient baseline is caught
    earlier and the component omitted.
    """
    if baseline.maturity is StatMaturity.MATURE:
        return RecoveryMaturity.MATURE
    return RecoveryMaturity.MIN
