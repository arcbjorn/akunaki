"""Golden formula tests for the recovery composite (general_recovery_v0.1.0).

Every expected value is computed by hand from health-engine.md. The formula
version is a stability contract: changing an output requires bumping the
version, so these are deliberately exact.
"""

from __future__ import annotations

import math

import pytest

from akunaki.domain.recovery import (
    COMPONENT_WEIGHTS,
    BaselineMaturity,
    ComponentCode,
    RecoveryComponent,
    RecoveryStatus,
    baseline_component_score,
    evaluate_recovery,
    freshness_one,
    sleep_target_adherence,
)


def _comp(
    code: ComponentCode,
    c: float,
    *,
    quality: str = "high",
    freshness_hours: float | None = 0.0,
    maturity: BaselineMaturity | None = None,
) -> RecoveryComponent:
    return RecoveryComponent(
        code=code,
        c=c,
        quality=quality,
        freshness_hours=freshness_hours,
        baseline_maturity=maturity,
    )


# ---------------------------------------------------------------------------
# Weights and invariants
# ---------------------------------------------------------------------------


def test_weights_sum_to_one() -> None:
    assert sum(COMPONENT_WEIGHTS.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Baseline z-score mapping
# ---------------------------------------------------------------------------


def test_zero_z_maps_to_midpoint() -> None:
    assert baseline_component_score(0.0) == 50.0


def test_positive_z_better_when_higher() -> None:
    # z=2, direction +1: 50 + 50*tanh(1) = 50 + 50*0.76159... = 88.0797...
    assert baseline_component_score(2.0) == pytest.approx(50 + 50 * math.tanh(1.0))


def test_direction_negative_inverts() -> None:
    # RHR is better-when-lower: a high z (bad) maps below 50.
    higher = baseline_component_score(2.0, direction=-1.0)
    assert higher == pytest.approx(50 + 50 * math.tanh(-1.0))
    assert higher < 50.0


def test_z_is_clamped_to_three() -> None:
    # Beyond |z|=3 the curve saturates; z=10 and z=3 give the same c.
    assert baseline_component_score(10.0) == baseline_component_score(3.0)
    assert baseline_component_score(-10.0) == baseline_component_score(-3.0)


# ---------------------------------------------------------------------------
# Adherence
# ---------------------------------------------------------------------------


def test_adherence_exact_target_is_full() -> None:
    assert sleep_target_adherence(duration_min=480, target_min=480) == 100.0


def test_adherence_no_oversleep_bonus() -> None:
    assert sleep_target_adherence(duration_min=600, target_min=480) == 100.0


def test_adherence_linear_shortfall() -> None:
    assert sleep_target_adherence(duration_min=360, target_min=480) == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_freshness_full_within_24h() -> None:
    assert freshness_one(0.0) == 1.0
    assert freshness_one(24.0) == 1.0


def test_freshness_half_at_72h() -> None:
    assert freshness_one(72.0) == pytest.approx(0.5)


def test_freshness_zero_at_168h() -> None:
    assert freshness_one(168.0) == pytest.approx(0.0)
    assert freshness_one(200.0) == 0.0


def test_freshness_midpoints_are_linear() -> None:
    # 48h: halfway from 1 (24h) to 0.5 (72h) -> 0.75.
    assert freshness_one(48.0) == pytest.approx(0.75)
    # 120h: halfway from 0.5 (72h) to 0 (168h) -> 0.25.
    assert freshness_one(120.0) == pytest.approx(0.25)


def test_future_timestamp_is_fresh() -> None:
    assert freshness_one(-5.0) == 1.0


# ---------------------------------------------------------------------------
# Sufficiency gate
# ---------------------------------------------------------------------------


def _sufficient_set() -> list[RecoveryComponent]:
    # Adherence (sleep group) + HRV, weights 0.20 + 0.25 = 0.45 < 0.60, so add
    # RHR (0.15) and temperature (0.10) to clear the 0.60 bar: total 0.70.
    return [
        _comp(ComponentCode.SLEEP_ADHERENCE, 100.0),
        _comp(ComponentCode.HRV, 100.0, maturity=BaselineMaturity.MATURE),
        _comp(ComponentCode.RESTING_HR, 100.0, maturity=BaselineMaturity.MATURE),
        _comp(ComponentCode.TEMPERATURE, 100.0, maturity=BaselineMaturity.MATURE),
    ]


def test_missing_sleep_adherence_is_insufficient() -> None:
    result = evaluate_recovery(
        [
            _comp(ComponentCode.HRV, 80.0),
            _comp(ComponentCode.RESTING_HR, 80.0),
            _comp(ComponentCode.TEMPERATURE, 80.0),
            _comp(ComponentCode.SLEEP_EFFICIENCY, 80.0),
        ]
    )
    assert result.status is RecoveryStatus.INSUFFICIENT
    assert result.score is None


def test_missing_hrv_and_rhr_is_insufficient() -> None:
    result = evaluate_recovery(
        [
            _comp(ComponentCode.SLEEP_ADHERENCE, 80.0),
            _comp(ComponentCode.SLEEP_EFFICIENCY, 80.0),
            _comp(ComponentCode.SLEEP_CONSISTENCY, 80.0),
            _comp(ComponentCode.TEMPERATURE, 80.0),
            _comp(ComponentCode.PRIOR_LOAD_BALANCE, 80.0),
        ]
    )
    assert result.status is RecoveryStatus.INSUFFICIENT
    assert result.score is None


def test_below_min_available_weight_is_insufficient() -> None:
    # Adherence 0.20 + HRV 0.25 = 0.45 < 0.60, even though gates 1 and 2 pass.
    result = evaluate_recovery(
        [
            _comp(ComponentCode.SLEEP_ADHERENCE, 90.0),
            _comp(ComponentCode.HRV, 90.0),
        ]
    )
    assert result.status is RecoveryStatus.INSUFFICIENT
    assert result.available_weight == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Weighted mean, coverage, status
# ---------------------------------------------------------------------------


def test_full_set_all_100_is_ok_score_100() -> None:
    full = [_comp(code, 100.0, maturity=BaselineMaturity.MATURE) for code in COMPONENT_WEIGHTS]
    result = evaluate_recovery(full)
    assert result.status is RecoveryStatus.OK
    assert result.score == 100
    assert result.available_weight == pytest.approx(1.0)
    assert result.confidence == pytest.approx(1.0)


def test_partial_set_renormalizes_over_present_weights() -> None:
    # HRV(0.25)=80, adherence(0.20)=60, RHR(0.15)=40, temp(0.10)=100.
    # available = 0.70; weighted = 0.25*80 + 0.20*60 + 0.15*40 + 0.10*100
    #           = 20 + 12 + 6 + 10 = 48; score = round(48/0.70) = round(68.57)=69.
    result = evaluate_recovery(
        [
            _comp(ComponentCode.HRV, 80.0, maturity=BaselineMaturity.MATURE),
            _comp(ComponentCode.SLEEP_ADHERENCE, 60.0),
            _comp(ComponentCode.RESTING_HR, 40.0, maturity=BaselineMaturity.MATURE),
            _comp(ComponentCode.TEMPERATURE, 100.0, maturity=BaselineMaturity.MATURE),
        ]
    )
    assert result.available_weight == pytest.approx(0.70)
    assert result.score == 69
    assert result.status is RecoveryStatus.PARTIAL


def test_missing_component_is_never_imputed() -> None:
    # A missing component must not act like a 50: two sets differing only by an
    # absent temperature must renormalize, not blend a midpoint.
    with_temp = evaluate_recovery(_sufficient_set())
    without_temp = evaluate_recovery(_sufficient_set()[:3])  # drop temperature
    # Both are all-100 present components -> both score 100 exactly.
    assert with_temp.score == 100
    assert without_temp.score == 100
    assert without_temp.available_weight == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# Confidence factors
# ---------------------------------------------------------------------------


def test_stale_critical_input_lowers_confidence() -> None:
    # HRV at 72h -> freshness 0.5; that is the min across critical inputs.
    result = evaluate_recovery(
        [
            _comp(ComponentCode.SLEEP_ADHERENCE, 100.0, freshness_hours=0.0),
            _comp(
                ComponentCode.HRV,
                100.0,
                freshness_hours=72.0,
                maturity=BaselineMaturity.MATURE,
            ),
            _comp(
                ComponentCode.RESTING_HR,
                100.0,
                freshness_hours=0.0,
                maturity=BaselineMaturity.MATURE,
            ),
            _comp(
                ComponentCode.TEMPERATURE,
                100.0,
                freshness_hours=0.0,
                maturity=BaselineMaturity.MATURE,
            ),
        ]
    )
    # coverage 0.70 * freshness 0.5 * quality 1.0 * maturity 1.0 = 0.35.
    assert result.confidence == pytest.approx(0.35)
    assert result.status is RecoveryStatus.PARTIAL


def test_min_baseline_maturity_lowers_confidence() -> None:
    # Full weight set, all fresh high quality, but HRV baseline only 'min'.
    # maturity factor = weighted over baseline-dependent components. Make only
    # HRV min and the rest mature to get a known number is complex; instead use
    # a set where the only baseline-dependent present component is HRV at min.
    result = evaluate_recovery(
        [
            _comp(ComponentCode.SLEEP_ADHERENCE, 100.0),  # no baseline
            _comp(ComponentCode.HRV, 100.0, maturity=BaselineMaturity.MIN),
            _comp(ComponentCode.RESTING_HR, 100.0),  # treat as no-baseline here
            _comp(ComponentCode.TEMPERATURE, 100.0),  # no baseline supplied
        ]
    )
    # Only HRV carries a maturity -> factor = 0.85. coverage 0.70, fresh 1.0,
    # quality 1.0 -> confidence = 0.70 * 0.85 = 0.595.
    assert result.confidence == pytest.approx(0.595)


def test_medium_quality_critical_input_lowers_confidence() -> None:
    result = evaluate_recovery(
        [
            _comp(ComponentCode.SLEEP_ADHERENCE, 100.0, quality="high"),
            _comp(ComponentCode.HRV, 100.0, quality="medium"),
            _comp(ComponentCode.RESTING_HR, 100.0, quality="high"),
            _comp(ComponentCode.TEMPERATURE, 100.0, quality="high"),
        ]
    )
    # critical quality mean = (1.0 + 0.75 + 1.0)/3 = 0.91666...
    # confidence = 0.70 * 1.0 * 0.91666... * 1.0 = 0.641666...
    assert result.confidence == pytest.approx(0.70 * (1.0 + 0.75 + 1.0) / 3.0)


# ---------------------------------------------------------------------------
# Factors and identity
# ---------------------------------------------------------------------------


def test_factors_disclose_present_and_absent() -> None:
    result = evaluate_recovery(_sufficient_set())
    by_code = {f.factor_code: f for f in result.factors}
    assert len(by_code) == len(COMPONENT_WEIGHTS)
    assert by_code["hrv"].present is True
    assert by_code["subjective"].present is False
    assert by_code["subjective"].magnitude == 0.0


def test_formula_version_pinned() -> None:
    assert evaluate_recovery(_sufficient_set()).formula_version == "general_recovery_v0.1.0"


def test_duplicate_component_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate component"):
        evaluate_recovery(
            [
                _comp(ComponentCode.HRV, 50.0),
                _comp(ComponentCode.HRV, 60.0),
            ]
        )
