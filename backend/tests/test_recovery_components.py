"""Tests for the feature -> recovery-component bridge (v0.1.0).

These verify the seam composes the baseline and recovery mappings correctly and
honors the cardinal rule: an insufficient baseline omits the component (returns
None), never a midpoint. Expected values are computed from health-engine.md.
"""

from __future__ import annotations

import math

import pytest

from akunaki.domain.baseline import MetricFamily
from akunaki.domain.recovery import (
    BaselineMaturity,
    ComponentCode,
    RecoveryStatus,
    evaluate_recovery,
)
from akunaki.domain.recovery_components import (
    BaselineInput,
    map_baseline_component,
    map_sleep_adherence_component,
)

# ---------------------------------------------------------------------------
# Baseline-dependent components
# ---------------------------------------------------------------------------


def test_higher_is_better_maps_above_midpoint() -> None:
    # HRV today above its own baseline center -> c above 50.
    signal = BaselineInput(
        value=120.0,
        samples=[90.0, 110.0] * 14,  # center 100, robust_scale 14.826
        family=MetricFamily.HRV,
        direction=1.0,
    )
    component = map_baseline_component(ComponentCode.HRV, signal)
    assert component is not None
    # z = 20 / 14.826 = 1.3489; c = 50 + 50*tanh(z/2).
    z = 20.0 / (1.4826 * 10.0)
    assert component.c == pytest.approx(50 + 50 * math.tanh(z / 2))
    assert component.c > 50.0
    assert component.baseline_maturity is BaselineMaturity.MATURE


def test_lower_is_better_inverts_direction() -> None:
    # RHR today above baseline (bad) with direction -1 -> c below 50.
    signal = BaselineInput(
        value=120.0,
        samples=[90.0, 110.0] * 14,
        family=MetricFamily.RHR,
        direction=-1.0,
    )
    component = map_baseline_component(ComponentCode.RESTING_HR, signal)
    assert component is not None
    assert component.c < 50.0


def test_insufficient_baseline_omits_component() -> None:
    # Only 10 prior samples -> baseline insufficient -> None (omit).
    signal = BaselineInput(
        value=100.0,
        samples=[100.0] * 10,
        family=MetricFamily.HRV,
        direction=1.0,
    )
    assert map_baseline_component(ComponentCode.HRV, signal) is None


def test_min_maturity_is_propagated() -> None:
    # 14-27 present samples -> min maturity flows onto the component.
    signal = BaselineInput(
        value=100.0,
        samples=[90.0, 110.0] * 7,  # 14 samples
        family=MetricFamily.HRV,
        direction=1.0,
    )
    component = map_baseline_component(ComponentCode.HRV, signal)
    assert component is not None
    assert component.baseline_maturity is BaselineMaturity.MIN


def test_quality_and_freshness_pass_through() -> None:
    signal = BaselineInput(
        value=100.0,
        samples=[90.0, 110.0] * 14,
        family=MetricFamily.HRV,
        direction=1.0,
        quality="medium",
        freshness_hours=30.0,
    )
    component = map_baseline_component(ComponentCode.HRV, signal)
    assert component is not None
    assert component.quality == "medium"
    assert component.freshness_hours == 30.0


# ---------------------------------------------------------------------------
# Direct sleep-target adherence
# ---------------------------------------------------------------------------


def test_adherence_component_is_direct_and_has_no_baseline() -> None:
    component = map_sleep_adherence_component(duration_min=360, target_min=480)
    assert component.code is ComponentCode.SLEEP_ADHERENCE
    assert component.c == pytest.approx(75.0)
    assert component.baseline_maturity is None


# ---------------------------------------------------------------------------
# End to end: sleep-only data is honestly insufficient
# ---------------------------------------------------------------------------


def test_sleep_only_inputs_are_insufficient() -> None:
    # Today's system has only sleep facts: adherence (+ maybe efficiency), but
    # no HRV or RHR. Gate 2 fails, so the honest outcome is insufficient.
    adherence = map_sleep_adherence_component(duration_min=420, target_min=480)
    efficiency_signal = BaselineInput(
        value=90.0,
        samples=[85.0, 95.0] * 14,
        family=MetricFamily.OTHER,
        direction=1.0,
    )
    efficiency = map_baseline_component(ComponentCode.SLEEP_EFFICIENCY, efficiency_signal)
    assert efficiency is not None
    result = evaluate_recovery([adherence, efficiency])
    assert result.status is RecoveryStatus.INSUFFICIENT
    assert result.score is None


def test_adding_hrv_clears_the_gate() -> None:
    # The same sleep inputs plus a usable HRV baseline reach a real score.
    adherence = map_sleep_adherence_component(
        duration_min=480, target_min=480, quality="high", freshness_hours=0.0
    )
    hrv = map_baseline_component(
        ComponentCode.HRV,
        BaselineInput(
            value=110.0,
            samples=[90.0, 110.0] * 14,
            family=MetricFamily.HRV,
            direction=1.0,
            quality="high",
            freshness_hours=0.0,
        ),
    )
    rhr = map_baseline_component(
        ComponentCode.RESTING_HR,
        BaselineInput(
            value=50.0,
            samples=[48.0, 52.0] * 14,
            family=MetricFamily.RHR,
            direction=-1.0,
            quality="high",
            freshness_hours=0.0,
        ),
    )
    assert hrv is not None and rhr is not None
    result = evaluate_recovery([adherence, hrv, rhr])
    # adherence 0.20 + HRV 0.25 + RHR 0.15 = 0.60 available weight (clears gate).
    assert result.available_weight == pytest.approx(0.60)
    assert result.status is not RecoveryStatus.INSUFFICIENT
    assert result.score is not None
