"""The read-only ``/v1/metrics/{metric}`` surface: one measured series.

A metric detail view shows the user's **own measurements** over a window, with
the baseline context the design requires charts to carry ("confidence, source,
and baseline context visible"). Every windowed query it reads already exists —
they are the same series the recovery components consume, so a chart and a
score can never disagree about what was measured.

Deliberately **measurements, not scores**. v0.1.0 ships exactly one score code
(recovery); presenting a per-metric number that looks like a rating would imply
a formula that has not been accepted. The baseline is disclosed as center and
dispersion so a chart can draw a band, never as a normalized "good/bad" value.

Missing days are **absent**, never zero: the queries omit unknown days, and a
zero-filled chart would show a real measurement of nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from akunaki.domain.baseline import Baseline, MetricFamily, compute_baseline

__all__ = [
    "MAX_WINDOW_DAYS",
    "SUPPORTED_METRICS",
    "MetricDefinition",
    "MetricNotFoundError",
    "MetricSeries",
    "MetricSeriesService",
    "MetricSource",
]

# The longest window a caller may ask for. Bounds the query and the response;
# the 42-day baseline window is the analytically meaningful span, so this is
# generous rather than restrictive.
MAX_WINDOW_DAYS = 180


class MetricNotFoundError(KeyError):
    """The requested metric is not one this build exposes."""


class MetricSource(Protocol):
    """Port: the windowed per-day feature queries."""

    def daily_hrv(self, *, tenant_id: str, local_health_days: list[str]) -> dict[str, float]:
        """Overnight HRV per day; omit days with no reading."""
        ...

    def daily_resting_hr(self, *, tenant_id: str, local_health_days: list[str]) -> dict[str, float]:
        """Overnight resting HR per day; omit days with no reading."""
        ...

    def daily_temperature_deviation(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Overnight temperature deviation per day; omit days with no reading."""
        ...

    def daily_respiratory_rate(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Overnight respiratory rate per day; omit days with no reading."""
        ...

    def daily_sleep_durations(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Total sleep minutes per day; omit days with no sleep."""
        ...

    def daily_sleep_efficiency(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Sleep efficiency percentage per day; omit undefined days."""
        ...

    def daily_activity_steps(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Daily step count; omit days with no steps."""
        ...

    def daily_strain_load(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Daily zone-weighted workout load; omit days with no workout."""
        ...


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """How one exposed metric is read and described."""

    metric: str
    query: str
    """Name of the ``MetricSource`` method supplying the series."""

    unit: str
    family: MetricFamily
    """Baseline family, which decides the robust-scale floor."""


# Every metric here is backed by a query the engine already reads, so a chart
# and a score cannot disagree about what was measured. A metric absent from this
# map is not exposed — an open string would let a caller probe for series that
# do not exist.
SUPPORTED_METRICS: dict[str, MetricDefinition] = {
    definition.metric: definition
    for definition in (
        MetricDefinition("hrv", "daily_hrv", "ms", MetricFamily.HRV),
        MetricDefinition("resting_hr", "daily_resting_hr", "bpm", MetricFamily.RHR),
        MetricDefinition(
            "temperature_deviation",
            "daily_temperature_deviation",
            "celsius",
            MetricFamily.TEMPERATURE,
        ),
        MetricDefinition(
            "respiratory_rate",
            "daily_respiratory_rate",
            "breaths_per_min",
            MetricFamily.RESPIRATORY,
        ),
        MetricDefinition(
            "sleep_duration", "daily_sleep_durations", "minutes", MetricFamily.SLEEP_DURATION
        ),
        MetricDefinition(
            "sleep_efficiency", "daily_sleep_efficiency", "percent", MetricFamily.OTHER
        ),
        MetricDefinition("steps", "daily_activity_steps", "steps", MetricFamily.ACTIVITY),
        MetricDefinition("strain_load", "daily_strain_load", "load", MetricFamily.OTHER),
    )
}


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One measured day."""

    local_health_day: str
    value: float


@dataclass(frozen=True, slots=True)
class MetricSeries:
    """A measured series plus the baseline context a chart needs."""

    metric: str
    unit: str
    window_days: int
    points: tuple[MetricPoint, ...]
    known_days: int
    baseline_maturity: str
    baseline_center: float | None
    baseline_robust_scale: float | None

    @property
    def coverage_is_partial(self) -> bool:
        """Whether the window has unknown days.

        Disclosed so a chart can say "12 of 30 days" rather than implying the
        gaps are measured zeros.
        """
        return self.known_days < self.window_days


class MetricSeriesService:
    """Build one metric's measured series for a tenant."""

    def __init__(self, *, source: MetricSource) -> None:
        self._source = source

    def series_for(
        self,
        *,
        tenant_id: str,
        metric: str,
        days: list[str],
    ) -> MetricSeries:
        """Return the metric's values across ``days``, oldest first.

        Raises :class:`MetricNotFoundError` for a metric this build does not
        expose, so an unknown name is a client error rather than an empty
        series that looks like "no data".
        """
        definition = SUPPORTED_METRICS.get(metric)
        if definition is None:
            raise MetricNotFoundError(metric)

        query = getattr(self._source, definition.query)
        by_day: dict[str, float] = query(tenant_id=tenant_id, local_health_days=days)

        points = tuple(
            MetricPoint(local_health_day=day, value=by_day[day]) for day in days if day in by_day
        )
        # The baseline is computed over the same present samples the engine
        # uses — no imputation, so a sparse window yields an honest
        # `insufficient` rather than a confident-looking band.
        baseline: Baseline = compute_baseline(
            [point.value for point in points], family=definition.family
        )
        return MetricSeries(
            metric=definition.metric,
            unit=definition.unit,
            window_days=len(days),
            points=points,
            known_days=len(points),
            baseline_maturity=baseline.maturity.value,
            baseline_center=baseline.center,
            baseline_robust_scale=baseline.robust_scale,
        )
