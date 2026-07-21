"""Assemble recovery components from a tenant's stored features.

This is the data-facing half of the scoring path: it fetches the windowed
prior-day feature series, builds the present components, and returns whatever is
available. The pure z-score/mapping/composite math lives in the domain.

Sleep-target adherence, sleep efficiency, overnight HRV, and overnight resting
HR are sourced today (all from the Oura sleep payload). A component is included
only when its input is known and — for baseline components — its 42-day
baseline is mature; otherwise it is omitted, never invented. The gate then
speaks: a day with HRV or RHR and enough coverage produces a real score; a day
without still returns ``insufficient``.
"""

from __future__ import annotations

from typing import Protocol

from akunaki.domain.baseline import MetricFamily, baseline_window_days
from akunaki.domain.recovery import (
    DEFAULT_SLEEP_TARGET_MIN,
    ComponentCode,
    Direction,
    RecoveryComponent,
)
from akunaki.domain.recovery_components import (
    BaselineInput,
    map_baseline_component,
    map_sleep_adherence_component,
)


class FeatureSource(Protocol):
    """Port: windowed daily features for a tenant.

    Each query returns a value per local day where the feature is known and
    omits every other day (missing days are absent, never imputed).
    """

    def daily_sleep_durations(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Total sleep minutes per known day."""
        ...

    def daily_sleep_efficiency(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Sleep efficiency percent per day where it is defined."""
        ...

    def daily_hrv(self, *, tenant_id: str, local_health_days: list[str]) -> dict[str, float]:
        """Overnight HRV (ms) per day where it is known."""
        ...

    def daily_resting_hr(self, *, tenant_id: str, local_health_days: list[str]) -> dict[str, float]:
        """Overnight resting HR (bpm) per day where it is known."""
        ...

    def daily_temperature_deviation(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Overnight temperature deviation (°C) per day where it is known."""
        ...

    def daily_respiratory_rate(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Overnight respiration rate (breaths/min) per day where it is known."""
        ...


class RecoveryInputService:
    """Build the present recovery components for a tenant's local day."""

    def __init__(self, *, features: FeatureSource) -> None:
        self._features = features

    def recovery_components(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        target_min: int = DEFAULT_SLEEP_TARGET_MIN,
    ) -> list[RecoveryComponent]:
        """Every recovery component present for the day (order-independent).

        Direct components (sleep-target adherence) are present whenever their
        input is known. Baseline components (efficiency, HRV, RHR) are present
        only when the day's value is known **and** its 42-day baseline is mature
        enough; otherwise they are omitted (never a midpoint).
        """
        window = baseline_window_days(local_health_day)
        span = [*window, local_health_day]
        components: list[RecoveryComponent] = []

        durations = self._features.daily_sleep_durations(
            tenant_id=tenant_id, local_health_days=span
        )
        today_duration = durations.get(local_health_day)
        if today_duration is not None:
            components.append(
                map_sleep_adherence_component(duration_min=today_duration, target_min=target_min)
            )

        self._add_baseline_component(
            components,
            code=ComponentCode.SLEEP_EFFICIENCY,
            series=self._features.daily_sleep_efficiency(
                tenant_id=tenant_id, local_health_days=span
            ),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.OTHER,
            direction=Direction.HIGHER_BETTER,
        )
        self._add_baseline_component(
            components,
            code=ComponentCode.HRV,
            series=self._features.daily_hrv(tenant_id=tenant_id, local_health_days=span),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.HRV,
            direction=Direction.HIGHER_BETTER,
        )
        self._add_baseline_component(
            components,
            code=ComponentCode.RESTING_HR,
            series=self._features.daily_resting_hr(tenant_id=tenant_id, local_health_days=span),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.RHR,
            direction=Direction.LOWER_BETTER,
        )
        self._add_baseline_component(
            components,
            code=ComponentCode.TEMPERATURE,
            series=self._features.daily_temperature_deviation(
                tenant_id=tenant_id, local_health_days=span
            ),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.TEMPERATURE,
            # Any deviation from baseline, in either direction, is worse.
            direction=Direction.DEVIATION_WORSE,
        )
        self._add_baseline_component(
            components,
            code=ComponentCode.RESPIRATORY,
            series=self._features.daily_respiratory_rate(
                tenant_id=tenant_id, local_health_days=span
            ),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.RESPIRATORY,
            # An elevated rate hurts; a low rate is not rewarded.
            direction=Direction.ELEVATED_WORSE,
        )

        return components

    @staticmethod
    def _add_baseline_component(
        components: list[RecoveryComponent],
        *,
        code: ComponentCode,
        series: dict[str, float],
        local_health_day: str,
        window: list[str],
        family: MetricFamily,
        direction: Direction,
    ) -> None:
        """Append a baseline component when today's value and a baseline exist."""
        today = series.get(local_health_day)
        if today is None:
            return
        prior = [series[day] for day in window if day in series]
        component = map_baseline_component(
            code,
            BaselineInput(value=today, samples=prior, family=family, direction=direction),
        )
        if component is not None:
            components.append(component)
