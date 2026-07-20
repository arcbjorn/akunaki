"""Golden tests for rolling baselines and robust z-scores (v0.1.0).

Expected values are computed by hand from health-engine.md. The statistics are
a stability contract for the scoring path, so these are deliberately exact.
"""

from __future__ import annotations

import statistics

import pytest

from akunaki.domain.baseline import (
    MATURE_SAMPLES,
    MIN_SAMPLES,
    WINDOW_DAYS,
    BaselineMaturity,
    MetricFamily,
    baseline_window_days,
    compute_baseline,
    z_score,
)

# ---------------------------------------------------------------------------
# Maturity gating
# ---------------------------------------------------------------------------


def test_below_14_samples_is_insufficient() -> None:
    baseline = compute_baseline([50.0] * (MIN_SAMPLES - 1))
    assert baseline.maturity is BaselineMaturity.INSUFFICIENT
    assert baseline.is_usable is False
    assert baseline.center is None
    assert baseline.robust_scale is None


def test_14_to_27_samples_is_min() -> None:
    # 14 and 27 both classify as 'min'.
    assert compute_baseline([50.0] * 14).maturity is BaselineMaturity.MIN
    assert compute_baseline([50.0] * 27).maturity is BaselineMaturity.MIN


def test_28_or_more_samples_is_mature() -> None:
    assert compute_baseline([50.0] * MATURE_SAMPLES).maturity is BaselineMaturity.MATURE
    assert compute_baseline([50.0] * 42).maturity is BaselineMaturity.MATURE


# ---------------------------------------------------------------------------
# Baseline window (calendar arithmetic)
# ---------------------------------------------------------------------------


def test_window_is_42_prior_days_excluding_target() -> None:
    window = baseline_window_days("2026-07-20")
    assert len(window) == WINDOW_DAYS
    assert window[-1] == "2026-07-19"  # D-1, the most recent prior day
    assert window[0] == "2026-06-08"  # D-42
    assert "2026-07-20" not in window  # target excluded from its own baseline
    assert window == sorted(window)  # oldest-first


def test_window_crosses_year_boundary() -> None:
    window = baseline_window_days("2026-01-10")
    assert window[-1] == "2026-01-09"
    assert window[0] == "2025-11-29"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_center_is_median() -> None:
    samples = [float(v) for v in range(1, 29)]  # 1..28, median = 14.5
    baseline = compute_baseline(samples)
    assert baseline.center == pytest.approx(14.5)


def test_robust_scale_is_mad_scaled() -> None:
    # 28 samples symmetric about 100 with a known MAD.
    samples = [90.0, 110.0] * 14  # median 100, |dev| all 10, MAD = 10
    baseline = compute_baseline(samples)
    assert baseline.center == pytest.approx(100.0)
    assert baseline.mad == pytest.approx(10.0)
    assert baseline.robust_scale == pytest.approx(1.4826 * 10.0)
    assert baseline.fallback_dispersion_used is False


def test_zero_mad_falls_back_to_iqr() -> None:
    # A majority at the center forces MAD = 0, but a spread of distinct tail
    # values keeps the quartiles apart, so IQR > 0 and the fallback triggers.
    samples = [100.0] * 15 + [
        40.0,
        50.0,
        60.0,
        70.0,
        80.0,
        90.0,
        110.0,
        120.0,
        130.0,
        140.0,
        150.0,
        160.0,
        170.0,
    ]
    baseline = compute_baseline(samples)
    assert baseline.mad == pytest.approx(0.0)
    assert baseline.robust_scale is not None
    iqr = baseline.p75 - baseline.p25  # type: ignore[operator]
    assert baseline.robust_scale == pytest.approx(iqr / 1.349)
    assert baseline.fallback_dispersion_used is True


def test_zero_mad_and_zero_iqr_falls_back_to_metric_floor() -> None:
    # Every sample identical: MAD = 0 and IQR = 0, so the family floor is used.
    baseline = compute_baseline([60.0] * 28, family=MetricFamily.RHR)
    assert baseline.mad == pytest.approx(0.0)
    assert baseline.p25 == baseline.p75
    assert baseline.robust_scale == pytest.approx(0.5)  # RHR floor
    assert baseline.fallback_dispersion_used is True


def test_metric_floor_differs_by_family() -> None:
    hrv = compute_baseline([50.0] * 28, family=MetricFamily.HRV)
    temp = compute_baseline([37.0] * 28, family=MetricFamily.TEMPERATURE)
    other = compute_baseline([5.0] * 28, family=MetricFamily.OTHER)
    assert hrv.robust_scale == pytest.approx(1.0)
    assert temp.robust_scale == pytest.approx(0.05)
    assert other.robust_scale == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# z-score
# ---------------------------------------------------------------------------


def test_z_score_matches_definition() -> None:
    samples = [90.0, 110.0] * 14  # center 100, robust_scale 14.826
    baseline = compute_baseline(samples)
    # value 120: z = (120 - 100) / 14.826 = 1.3489...
    assert z_score(120.0, baseline) == pytest.approx(20.0 / (1.4826 * 10.0))


def test_z_score_is_clamped_to_three() -> None:
    samples = [99.0, 101.0] * 14  # center 100, robust_scale = 1.4826
    baseline = compute_baseline(samples)
    # value 200 would be z ~= 67; clamped to +3.
    assert z_score(200.0, baseline) == pytest.approx(3.0)
    assert z_score(-200.0, baseline) == pytest.approx(-3.0)


def test_z_score_zero_at_center() -> None:
    baseline = compute_baseline([90.0, 110.0] * 14)
    assert z_score(100.0, baseline) == pytest.approx(0.0)


def test_z_score_rejects_insufficient_baseline() -> None:
    baseline = compute_baseline([50.0] * 3)
    with pytest.raises(ValueError, match="insufficient baseline"):
        z_score(60.0, baseline)


# ---------------------------------------------------------------------------
# No imputation
# ---------------------------------------------------------------------------


def test_missing_days_are_absent_not_zero() -> None:
    # The series carries only present days; a caller that dropped 10 missing
    # days from a 24-day span yields 14 samples -> min, not 24 with zeros.
    present = [480.0] * 14
    baseline = compute_baseline(present, family=MetricFamily.SLEEP_DURATION)
    assert baseline.sample_count == 14
    assert baseline.maturity is BaselineMaturity.MIN
    # If zeros had been imputed the center would be pulled far below 480.
    assert baseline.center == pytest.approx(480.0)


def test_center_and_mad_agree_with_stdlib() -> None:
    # Cross-check against statistics for an asymmetric real-ish series.
    samples = [
        42.0,
        45.0,
        47.0,
        50.0,
        51.0,
        53.0,
        55.0,
        58.0,
        60.0,
        61.0,
        63.0,
        65.0,
        68.0,
        70.0,
        72.0,
        40.0,
    ]
    baseline = compute_baseline(samples, family=MetricFamily.HRV)
    center = statistics.median(samples)
    assert baseline.center == pytest.approx(center)
    assert baseline.mad == pytest.approx(statistics.median([abs(s - center) for s in samples]))
