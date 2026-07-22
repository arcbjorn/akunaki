"""Golden tests for anomaly detectors (v0.1.0).

Thresholds are a stability contract; every boundary is checked exactly against
health-engine.md.
"""

from __future__ import annotations

from akunaki.domain.anomalies import (
    AnomalyCode,
    AnomalySeverity,
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
