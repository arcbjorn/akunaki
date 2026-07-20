"""Rolling baselines and robust z-scores (``general_recovery_v0.1.0``).

Pure: no I/O, no clock. The caller supplies the prior-day sample series (the
current day is excluded from its own baseline center). These are the exact
v0.1.0 statistics from health-engine.md.

A baseline is robust by design: median center, MAD-based scale with an IQR
fallback and per-metric floors, and a maturity gate. Missing days are never
imputed — they are simply absent from the series. When fewer than 14 samples
are present the baseline is ``insufficient`` and the dependent component is
omitted upstream rather than invented at a midpoint.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum

# Sample-count thresholds (present, quality-eligible points in the window).
MIN_SAMPLES = 14
MATURE_SAMPLES = 28

# MAD and IQR are scaled to a sigma-equivalent by these constants.
_MAD_TO_SIGMA = 1.4826
_IQR_TO_SIGMA = 1.349

# z is clamped to this range before any directed mapping.
_Z_CLAMP = 3.0


class MetricFamily(StrEnum):
    """Metric families with distinct robust-scale floors."""

    HRV = "hrv"
    RHR = "rhr"
    SLEEP_DURATION = "sleep_duration"
    TEMPERATURE = "temperature"
    RESPIRATORY = "respiratory"
    ACTIVITY = "activity"
    OTHER = "other"


# Robust-scale floor per family (used only when MAD and IQR are both zero/null).
_METRIC_FLOOR: dict[MetricFamily, float] = {
    MetricFamily.HRV: 1.0,
    MetricFamily.RHR: 0.5,
    MetricFamily.SLEEP_DURATION: 5.0,
    MetricFamily.TEMPERATURE: 0.05,
    MetricFamily.RESPIRATORY: 0.2,
    MetricFamily.ACTIVITY: 100.0,
    MetricFamily.OTHER: 1.0,
}


class BaselineMaturity(StrEnum):
    """Maturity of a computed baseline."""

    INSUFFICIENT = "insufficient"
    MIN = "min"
    MATURE = "mature"


@dataclass(frozen=True, slots=True)
class Baseline:
    """A computed rolling baseline for one feature.

    ``center``, ``mad``, ``robust_scale``, and the percentiles are None only
    when the baseline is ``insufficient`` (fewer than 14 present samples).
    """

    maturity: BaselineMaturity
    sample_count: int
    center: float | None = None
    mad: float | None = None
    p25: float | None = None
    p75: float | None = None
    robust_scale: float | None = None
    fallback_dispersion_used: bool = False

    @property
    def is_usable(self) -> bool:
        """Whether this baseline can produce a z-score for a recovery component."""
        return self.maturity is not BaselineMaturity.INSUFFICIENT


def compute_baseline(
    samples: list[float],
    *,
    family: MetricFamily = MetricFamily.OTHER,
) -> Baseline:
    """Compute a rolling baseline from prior-day present samples.

    ``samples`` is the present, quality-eligible series over the 42-day window
    ending at ``D-1``; missing days are already excluded (never zero-imputed).
    The caller is responsible for windowing and stratification.
    """
    count = len(samples)
    if count < MIN_SAMPLES:
        return Baseline(maturity=BaselineMaturity.INSUFFICIENT, sample_count=count)

    center = statistics.median(samples)
    mad = statistics.median([abs(s - center) for s in samples])
    p25, p75 = _quartiles(samples)
    robust_scale, fallback = _robust_scale(mad=mad, p25=p25, p75=p75, family=family)

    maturity = BaselineMaturity.MATURE if count >= MATURE_SAMPLES else BaselineMaturity.MIN
    return Baseline(
        maturity=maturity,
        sample_count=count,
        center=center,
        mad=mad,
        p25=p25,
        p75=p75,
        robust_scale=robust_scale,
        fallback_dispersion_used=fallback,
    )


def z_score(value: float, baseline: Baseline) -> float:
    """Robust z-score of ``value`` against a usable baseline, clamped to [-3, 3].

    Raises if the baseline is insufficient — the caller must omit the component
    instead, never coerce a z from an unusable baseline.
    """
    if not baseline.is_usable or baseline.center is None or baseline.robust_scale is None:
        msg = "cannot compute z from an insufficient baseline"
        raise ValueError(msg)
    raw = (value - baseline.center) / baseline.robust_scale
    return max(-_Z_CLAMP, min(_Z_CLAMP, raw))


def _quartiles(samples: list[float]) -> tuple[float, float]:
    """Return (p25, p75). Falls back to min/max when too few points for quantiles."""
    if len(samples) < 2:
        only = samples[0]
        return only, only
    # statistics.quantiles needs n >= 2; n=4 gives the 25/50/75 cut points.
    cuts = statistics.quantiles(samples, n=4, method="inclusive")
    return cuts[0], cuts[2]


def _robust_scale(
    *,
    mad: float,
    p25: float,
    p75: float,
    family: MetricFamily,
) -> tuple[float, bool]:
    """Sigma-equivalent scale via the MAD → IQR → metric-floor fallback chain.

    Returns (robust_scale, fallback_dispersion_used). The chain stops at the
    first usable (strictly positive) scale.
    """
    if mad > 0.0:
        return _MAD_TO_SIGMA * mad, False
    iqr = p75 - p25
    if iqr > 0.0:
        return iqr / _IQR_TO_SIGMA, True
    return _METRIC_FLOOR[family], True
