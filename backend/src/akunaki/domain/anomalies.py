"""Anomaly detectors: per-day open conditions and severity (v0.1.0).

Pure: no I/O, no clock. These are the exact v0.1.0 detector thresholds from
health-engine.md. Each detector inspects a metric's robust z-score (computed
upstream by the baseline layer) and decides whether an anomaly is **open** for
the day, and at what severity.

Anomalies are **descriptive**, never diagnostic or predictive. Only the six
enabled v0.1.0 detectors are implemented; the deferred detectors (abnormal
workout HR, multi-day decline, sudden change) are intentionally absent rather
than stubbed with empty thresholds.

The **clear** rule (metric returned to |z_dir| < 1.5 for 2 consecutive days)
is stateful across days and belongs to the anomaly-persistence layer; this
module answers the pure, single-day question of whether the open condition
holds and how severe it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

RULESET_VERSION = "anomaly_v0.1.0"

# Open threshold on the directed z; severity high when at the z clamp.
_OPEN_Z = 2.5
_CLAMP_Z = 3.0
# Short sleep opens on a shortfall of at least this many minutes.
_SHORT_SLEEP_SHORTFALL_MIN = 120.0


class AnomalyCode(StrEnum):
    """The six enabled v0.1.0 anomaly detectors."""

    LOW_HRV = "low_hrv"
    ELEVATED_RHR = "elevated_rhr"
    DEVIANT_TEMPERATURE = "deviant_temperature"
    ELEVATED_RESPIRATION = "elevated_respiration"
    LOW_ACTIVITY = "low_activity"
    SHORT_SLEEP = "short_sleep"


class AnomalySeverity(StrEnum):
    """Anomaly severity: moderate below the clamp, high at it."""

    MODERATE = "moderate"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Anomaly:
    """A detected open anomaly for one local health day."""

    code: AnomalyCode
    severity: AnomalySeverity


def _severity(z_magnitude: float) -> AnomalySeverity:
    """High when the metric sits at the z clamp (|z| >= 3), else moderate."""
    return AnomalySeverity.HIGH if z_magnitude >= _CLAMP_Z else AnomalySeverity.MODERATE


def detect_low_hrv(z_hrv: float) -> Anomaly | None:
    """Open when HRV is far below baseline (z <= -2.5)."""
    if z_hrv <= -_OPEN_Z:
        return Anomaly(code=AnomalyCode.LOW_HRV, severity=_severity(abs(z_hrv)))
    return None


def detect_elevated_rhr(z_rhr: float) -> Anomaly | None:
    """Open when resting HR is far above baseline (z >= 2.5)."""
    if z_rhr >= _OPEN_Z:
        return Anomaly(code=AnomalyCode.ELEVATED_RHR, severity=_severity(abs(z_rhr)))
    return None


def detect_deviant_temperature(z_temp: float) -> Anomaly | None:
    """Open when temperature deviates far in either direction (|z| >= 2.5)."""
    if abs(z_temp) >= _OPEN_Z:
        return Anomaly(code=AnomalyCode.DEVIANT_TEMPERATURE, severity=_severity(abs(z_temp)))
    return None


def detect_elevated_respiration(z_resp: float) -> Anomaly | None:
    """Open when respiration is far above baseline (z >= 2.5)."""
    if z_resp >= _OPEN_Z:
        return Anomaly(code=AnomalyCode.ELEVATED_RESPIRATION, severity=_severity(abs(z_resp)))
    return None


def detect_low_activity(z_activity: float) -> Anomaly | None:
    """Open when activity is far below baseline (z <= -2.5)."""
    if z_activity <= -_OPEN_Z:
        return Anomaly(code=AnomalyCode.LOW_ACTIVITY, severity=_severity(abs(z_activity)))
    return None


def detect_short_sleep(*, shortfall_min: float, z_sleep_duration: float) -> Anomaly | None:
    """Open when sleep is far short of target: a 120-min shortfall **or** z <= -2.5.

    Either condition opens. Severity uses the sleep-duration z magnitude, so a
    large-shortfall night with a clamped z reads as high.
    """
    if shortfall_min >= _SHORT_SLEEP_SHORTFALL_MIN or z_sleep_duration <= -_OPEN_Z:
        return Anomaly(
            code=AnomalyCode.SHORT_SLEEP,
            severity=_severity(abs(z_sleep_duration)),
        )
    return None
