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

# Clear when the metric has returned within this directed-z magnitude...
_CLEAR_Z = 1.5
# ...for this many consecutive local health days.
_CLEAR_CONSECUTIVE_DAYS = 2


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


# ---------------------------------------------------------------------------
# Stateful open/clear tracking
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnomalyState:
    """One anomaly's tracked state across days.

    ``is_open`` is whether the interval is currently open; ``severity`` is the
    peak severity while open; ``consecutive_clear_days`` counts how many recent
    days in a row met the clear condition (reset to 0 whenever it does not).
    """

    is_open: bool
    severity: AnomalySeverity | None
    consecutive_clear_days: int


@dataclass(frozen=True, slots=True)
class AnomalyTransition:
    """The result of advancing an anomaly one day."""

    state: AnomalyState
    opened: bool
    """True when the interval opened on this day (was closed, now open)."""
    cleared: bool
    """True when the interval cleared on this day (was open, now closed)."""


def advance_anomaly(
    *,
    prior: AnomalyState | None,
    open_today: Anomaly | None,
    clear_today: bool,
) -> AnomalyTransition:
    """Advance one anomaly one day (pure state machine).

    ``open_today`` is the detector's per-day result; ``clear_today`` is whether
    the direction-aware clear condition holds for the metric today. An interval
    opens on the first day the open condition holds, and clears only after the
    clear condition has held for two consecutive days.
    """
    was_open = prior is not None and prior.is_open
    prior_severity = prior.severity if prior is not None else None
    prior_clear_run = prior.consecutive_clear_days if prior is not None else 0

    if not was_open:
        if open_today is not None:
            return AnomalyTransition(
                state=AnomalyState(
                    is_open=True,
                    severity=open_today.severity,
                    consecutive_clear_days=0,
                ),
                opened=True,
                cleared=False,
            )
        # Stays closed; no interval to track.
        return AnomalyTransition(
            state=AnomalyState(is_open=False, severity=None, consecutive_clear_days=0),
            opened=False,
            cleared=False,
        )

    # Currently open: re-opening today refreshes severity and resets the clear
    # run; otherwise count clear days toward the 2-day clear.
    if open_today is not None:
        severity = _max_severity(prior_severity, open_today.severity)
        return AnomalyTransition(
            state=AnomalyState(is_open=True, severity=severity, consecutive_clear_days=0),
            opened=False,
            cleared=False,
        )

    clear_run = prior_clear_run + 1 if clear_today else 0
    if clear_run >= _CLEAR_CONSECUTIVE_DAYS:
        return AnomalyTransition(
            state=AnomalyState(is_open=False, severity=None, consecutive_clear_days=0),
            opened=False,
            cleared=True,
        )
    # Still open, accumulating clear days (or reset on a non-clear day).
    return AnomalyTransition(
        state=AnomalyState(
            is_open=True,
            severity=prior_severity,
            consecutive_clear_days=clear_run,
        ),
        opened=False,
        cleared=False,
    )


def clears_low_hrv(z_hrv: float) -> bool:
    """HRV clears when z has risen back above -1.5."""
    return z_hrv > -_CLEAR_Z


def clears_elevated_rhr(z_rhr: float) -> bool:
    """RHR clears when z has fallen back below 1.5."""
    return z_rhr < _CLEAR_Z


def clears_deviant_temperature(z_temp: float) -> bool:
    """Temperature clears when |z| has fallen back below 1.5."""
    return abs(z_temp) < _CLEAR_Z


def clears_elevated_respiration(z_resp: float) -> bool:
    """Respiration clears when z has fallen back below 1.5."""
    return z_resp < _CLEAR_Z


def clears_low_activity(z_activity: float) -> bool:
    """Activity clears when z has risen back above -1.5."""
    return z_activity > -_CLEAR_Z


def clears_short_sleep(*, shortfall_min: float, z_sleep_duration: float) -> bool:
    """Short sleep clears when shortfall < 120 **and** z has risen above -1.5."""
    return shortfall_min < _SHORT_SLEEP_SHORTFALL_MIN and z_sleep_duration > -_CLEAR_Z


def _max_severity(a: AnomalySeverity | None, b: AnomalySeverity | None) -> AnomalySeverity | None:
    """The higher of two severities (high > moderate); None only if both None."""
    order = {None: -1, AnomalySeverity.MODERATE: 0, AnomalySeverity.HIGH: 1}
    return a if order[a] >= order[b] else b
