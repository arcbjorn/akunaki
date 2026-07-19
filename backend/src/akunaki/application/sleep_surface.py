"""The read-only ``/v1/sleep`` day surface.

This assembles the deterministic sleep summary for one local day: it fetches
the per-day sleep durations across the 14-day debt window, then hands them to
the pure domain builder. A day with no known sleep is left out of the window
map and becomes an unknown day (never imputed as zero), which the debt
algorithm skips and discloses as a lower bound.

Sleep ships as a deterministic summary, not a score. Nothing here computes or
exposes a "sleep score"; the surface carries duration, target, adherence, and
disclosed debt only.
"""

from __future__ import annotations

from typing import Protocol

from akunaki.domain.sleep_summary import (
    DEFAULT_TARGET_MIN,
    DailySleep,
    SleepSummary,
    build_sleep_summary,
    debt_window_days,
)


class DailySleepDurationSource(Protocol):
    """Port: total known sleep minutes per local day for a tenant."""

    def daily_sleep_durations(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, float]:
        """Map each day with known sleep to its total minutes; omit unknowns."""
        ...


class SleepSurfaceService:
    """Build the deterministic sleep summary for a tenant's local day."""

    def __init__(self, *, durations: DailySleepDurationSource) -> None:
        self._durations = durations

    def summary_for_day(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        target_min: int = DEFAULT_TARGET_MIN,
    ) -> SleepSummary:
        """Assemble the summary from the 14-day debt window ending on the day."""
        window_days = debt_window_days(local_health_day)
        by_day = self._durations.daily_sleep_durations(
            tenant_id=tenant_id,
            local_health_days=window_days,
        )
        window = [
            DailySleep(local_health_day=day, duration_min=by_day.get(day)) for day in window_days
        ]
        # The target day's own duration drives adherence; unknown -> 0 duration,
        # which reads as full shortfall for that day (the design's default when
        # a day is present in the surface but has no recorded sleep).
        duration_min = by_day.get(local_health_day, 0.0)
        return build_sleep_summary(
            local_health_day=local_health_day,
            duration_min=duration_min,
            window=window,
            target_min=target_min,
        )
