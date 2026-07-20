"""Assemble recovery components from a tenant's stored sleep features.

This is the data-facing half of the scoring path: it fetches the windowed
prior-day feature series, builds the sleep components, and returns whatever is
present. The pure z-score/mapping/composite math lives in the domain.

Today only sleep facts exist, so this produces at most sleep-target adherence
and sleep efficiency. Neither satisfies the recovery gate's HRV-or-RHR
requirement, so ``evaluate_recovery`` over these inputs is honestly
``insufficient`` until wearable HRV/RHR ingestion lands. This module does not
paper over that — it returns the real (small) component set and lets the gate
speak.
"""

from __future__ import annotations

from typing import Protocol

from akunaki.domain.baseline import MetricFamily, baseline_window_days
from akunaki.domain.recovery import DEFAULT_SLEEP_TARGET_MIN, ComponentCode, RecoveryComponent
from akunaki.domain.recovery_components import (
    BaselineInput,
    map_baseline_component,
    map_sleep_adherence_component,
)


class SleepFeatureSource(Protocol):
    """Port: windowed daily sleep features for a tenant."""

    def daily_sleep_durations(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Total sleep minutes per known day; omit days with no known sleep."""
        ...

    def daily_sleep_efficiency(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Sleep efficiency percent per day where it is defined; omit others."""
        ...


class RecoveryInputService:
    """Build the present recovery components for a tenant's local day."""

    def __init__(self, *, features: SleepFeatureSource) -> None:
        self._features = features

    def sleep_components(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        target_min: int = DEFAULT_SLEEP_TARGET_MIN,
    ) -> list[RecoveryComponent]:
        """The sleep-derived components present for the day (order-independent).

        Sleep-target adherence is present whenever the day has a known sleep
        duration. Sleep efficiency is present only when the day's efficiency is
        known **and** its 42-day baseline is mature enough; otherwise it is
        omitted (never a midpoint).
        """
        window = baseline_window_days(local_health_day)
        # Fetch the target day's own values and the prior window in one span.
        span = [*window, local_health_day]
        durations = self._features.daily_sleep_durations(
            tenant_id=tenant_id, local_health_days=span
        )
        efficiencies = self._features.daily_sleep_efficiency(
            tenant_id=tenant_id, local_health_days=span
        )

        components: list[RecoveryComponent] = []

        today_duration = durations.get(local_health_day)
        if today_duration is not None:
            components.append(
                map_sleep_adherence_component(duration_min=today_duration, target_min=target_min)
            )

        today_efficiency = efficiencies.get(local_health_day)
        if today_efficiency is not None:
            prior_series = [efficiencies[day] for day in window if day in efficiencies]
            component = map_baseline_component(
                ComponentCode.SLEEP_EFFICIENCY,
                BaselineInput(
                    value=today_efficiency,
                    samples=prior_series,
                    family=MetricFamily.OTHER,
                    direction=1.0,
                ),
            )
            if component is not None:
                components.append(component)

        return components
