"""Golden tests for anomaly detectors (v0.1.0).

Thresholds are a stability contract; every boundary is checked exactly against
health-engine.md.
"""

from __future__ import annotations

from akunaki.domain.anomalies import (
    Anomaly,
    AnomalyCode,
    AnomalySeverity,
    advance_anomaly,
    clears_deviant_temperature,
    clears_low_hrv,
    clears_short_sleep,
    detect_deviant_temperature,
    detect_elevated_respiration,
    detect_elevated_rhr,
    detect_low_activity,
    detect_low_hrv,
    detect_short_sleep,
)

# ---------------------------------------------------------------------------
# Open conditions at the boundary
# ---------------------------------------------------------------------------


def test_low_hrv_opens_at_minus_2_5() -> None:
    assert detect_low_hrv(-2.5) is not None
    assert detect_low_hrv(-2.49) is None
    anomaly = detect_low_hrv(-2.7)
    assert anomaly is not None
    assert anomaly.code is AnomalyCode.LOW_HRV


def test_elevated_rhr_opens_at_plus_2_5() -> None:
    assert detect_elevated_rhr(2.5) is not None
    assert detect_elevated_rhr(2.49) is None
    # A low RHR (good) never opens this detector.
    assert detect_elevated_rhr(-3.0) is None


def test_deviant_temperature_is_absolute() -> None:
    # Either direction opens temperature.
    assert detect_deviant_temperature(2.6) is not None
    assert detect_deviant_temperature(-2.6) is not None
    assert detect_deviant_temperature(1.0) is None


def test_elevated_respiration_opens_at_plus_2_5() -> None:
    assert detect_elevated_respiration(2.5) is not None
    assert detect_elevated_respiration(-3.0) is None


def test_low_activity_opens_below_minus_2_5() -> None:
    assert detect_low_activity(-2.5) is not None
    assert detect_low_activity(3.0) is None


# ---------------------------------------------------------------------------
# Short sleep: two open conditions
# ---------------------------------------------------------------------------


def test_short_sleep_opens_on_shortfall() -> None:
    # 120-min shortfall opens even with a benign z.
    anomaly = detect_short_sleep(shortfall_min=120.0, z_sleep_duration=0.0)
    assert anomaly is not None
    assert anomaly.code is AnomalyCode.SHORT_SLEEP


def test_short_sleep_opens_on_z() -> None:
    # A large negative z opens even with a small shortfall.
    assert detect_short_sleep(shortfall_min=30.0, z_sleep_duration=-2.5) is not None


def test_short_sleep_stays_closed_when_neither_holds() -> None:
    assert detect_short_sleep(shortfall_min=60.0, z_sleep_duration=-1.0) is None


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def test_moderate_below_the_clamp() -> None:
    anomaly = detect_low_hrv(-2.7)
    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.MODERATE


def test_high_at_the_clamp() -> None:
    anomaly = detect_low_hrv(-3.0)
    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.HIGH


def test_temperature_severity_uses_absolute_z() -> None:
    anomaly = detect_deviant_temperature(-3.0)
    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.HIGH


# ---------------------------------------------------------------------------
# Clear conditions (direction-aware)
# ---------------------------------------------------------------------------


def test_hrv_clears_above_minus_1_5() -> None:
    assert clears_low_hrv(-1.4) is True
    assert clears_low_hrv(-1.5) is False  # boundary is exclusive
    assert clears_low_hrv(-2.0) is False


def test_temperature_clears_within_1_5_absolute() -> None:
    assert clears_deviant_temperature(1.4) is True
    assert clears_deviant_temperature(-1.4) is True
    assert clears_deviant_temperature(1.6) is False


def test_short_sleep_needs_both_shortfall_and_z_to_clear() -> None:
    assert clears_short_sleep(shortfall_min=60.0, z_sleep_duration=0.0) is True
    # Small shortfall but still a low z -> not cleared.
    assert clears_short_sleep(shortfall_min=60.0, z_sleep_duration=-2.0) is False
    # Good z but a large shortfall -> not cleared.
    assert clears_short_sleep(shortfall_min=130.0, z_sleep_duration=0.0) is False


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


_HIGH = Anomaly(code=AnomalyCode.LOW_HRV, severity=AnomalySeverity.HIGH)
_MOD = Anomaly(code=AnomalyCode.LOW_HRV, severity=AnomalySeverity.MODERATE)


def test_opens_from_closed() -> None:
    result = advance_anomaly(prior=None, open_today=_MOD, clear_today=False)
    assert result.opened is True
    assert result.state.is_open is True
    assert result.state.severity is AnomalySeverity.MODERATE


def test_stays_closed_when_condition_absent() -> None:
    result = advance_anomaly(prior=None, open_today=None, clear_today=True)
    assert result.opened is False
    assert result.state.is_open is False


def test_needs_two_consecutive_clear_days() -> None:
    opened = advance_anomaly(prior=None, open_today=_MOD, clear_today=False).state
    # Day 1 clear: one clear day, still open.
    day1 = advance_anomaly(prior=opened, open_today=None, clear_today=True)
    assert day1.cleared is False
    assert day1.state.is_open is True
    assert day1.state.consecutive_clear_days == 1
    # Day 2 clear: two in a row -> cleared.
    day2 = advance_anomaly(prior=day1.state, open_today=None, clear_today=True)
    assert day2.cleared is True
    assert day2.state.is_open is False


def test_non_clear_day_resets_the_run() -> None:
    opened = advance_anomaly(prior=None, open_today=_MOD, clear_today=False).state
    day1 = advance_anomaly(prior=opened, open_today=None, clear_today=True).state
    assert day1.consecutive_clear_days == 1
    # A day that neither opens nor clears resets the run to 0.
    day2 = advance_anomaly(prior=day1, open_today=None, clear_today=False)
    assert day2.state.consecutive_clear_days == 0
    assert day2.state.is_open is True


def test_reopen_refreshes_severity_and_resets_clear_run() -> None:
    opened = advance_anomaly(prior=None, open_today=_MOD, clear_today=False).state
    clear1 = advance_anomaly(prior=opened, open_today=None, clear_today=True).state
    assert clear1.consecutive_clear_days == 1
    # Re-opening (now high) resets the run and raises the peak severity.
    reopened = advance_anomaly(prior=clear1, open_today=_HIGH, clear_today=False)
    assert reopened.state.consecutive_clear_days == 0
    assert reopened.state.severity is AnomalySeverity.HIGH
    assert reopened.opened is False  # already open, not a new interval


def test_peak_severity_is_retained_across_days() -> None:
    opened = advance_anomaly(prior=None, open_today=_HIGH, clear_today=False).state
    # A moderate re-open does not lower the retained peak.
    result = advance_anomaly(prior=opened, open_today=_MOD, clear_today=False)
    assert result.state.severity is AnomalySeverity.HIGH
