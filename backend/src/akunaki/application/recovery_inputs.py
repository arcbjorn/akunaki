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

from collections.abc import Callable
from datetime import date, timedelta
from typing import Protocol

from akunaki.application.anomaly_tracker import FeatureSignal
from akunaki.domain.anomalies import (
    Anomaly,
    AnomalyCode,
    clears_deviant_temperature,
    clears_elevated_respiration,
    clears_elevated_rhr,
    clears_low_hrv,
    clears_short_sleep,
    detect_deviant_temperature,
    detect_elevated_respiration,
    detect_elevated_rhr,
    detect_low_hrv,
    detect_short_sleep,
)
from akunaki.domain.baseline import MetricFamily, baseline_window_days
from akunaki.domain.prior_load import ACUTE_WINDOW_DAYS, CHRONIC_WINDOW_DAYS
from akunaki.domain.recovery import (
    DEFAULT_SLEEP_TARGET_MIN,
    ComponentCode,
    Direction,
    RecoveryComponent,
)
from akunaki.domain.recovery_components import (
    BaselineInput,
    baseline_z_for,
    map_baseline_component,
    map_prior_load_component,
    map_sleep_adherence_component,
    map_sleep_consistency_component,
)
from akunaki.domain.sleep_summary import debt_window_days
from akunaki.domain.subjective import SubjectiveInputs, subjective_component


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

    def daily_principal_sleep_midpoint(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Principal-sleep local midpoint (minutes) per day where a valid night exists."""
        ...

    def daily_strain_load(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        """Daily strain-load per day where it is known (confirmed rest is 0.0)."""
        ...


class SubjectiveSource(Protocol):
    """Port: the current completed check-in's normalized inputs for a day."""

    def current_check_in_inputs(
        self, *, tenant_id: str, local_health_day: str
    ) -> SubjectiveInputs | None:
        """Return the day's completed check-in inputs, or None when absent."""
        ...


class RecoveryInputService:
    """Build the present recovery components for a tenant's local day."""

    def __init__(
        self,
        *,
        features: FeatureSource,
        subjective: SubjectiveSource | None = None,
    ) -> None:
        self._features = features
        self._subjective = subjective

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

        # Sleep consistency uses circular statistics over the 14-day debt window
        # (not the 42-day baseline window) and no baseline; it needs >= 7 valid
        # nights or it is omitted.
        consistency_window = debt_window_days(local_health_day)
        midpoints = self._features.daily_principal_sleep_midpoint(
            tenant_id=tenant_id, local_health_days=consistency_window
        )
        consistency = map_sleep_consistency_component(
            [midpoints[day] for day in consistency_window if day in midpoints]
        )
        if consistency is not None:
            components.append(consistency)

        # Prior-load balance: descriptive ACWR over the 7-day acute and 28-day
        # chronic windows (both ending on the target day). Strict coverage — any
        # unknown day makes ACWR undefined and the component is omitted. Daily
        # strain-load has no source yet (needs Polar zone data), so today every
        # day is unknown and the component is always omitted, honestly.
        chronic_window = _window_ending(local_health_day, CHRONIC_WINDOW_DAYS)
        loads = self._features.daily_strain_load(
            tenant_id=tenant_id, local_health_days=chronic_window
        )
        acute_window = chronic_window[-ACUTE_WINDOW_DAYS:]
        prior_load = map_prior_load_component(
            acute_daily_loads=[loads.get(day) for day in acute_window],
            chronic_daily_loads=[loads.get(day) for day in chronic_window],
        )
        if prior_load is not None:
            components.append(prior_load)

        # Subjective: only a completed check-in with all three normalized fields
        # present contributes; a missing check-in or blank field omits it (never
        # a neutral midpoint). Absent unless a subjective source is wired.
        if self._subjective is not None:
            inputs = self._subjective.current_check_in_inputs(
                tenant_id=tenant_id, local_health_day=local_health_day
            )
            if inputs is not None:
                subjective = subjective_component(inputs)
                if subjective.present and subjective.c is not None:
                    components.append(
                        RecoveryComponent(code=ComponentCode.SUBJECTIVE, c=subjective.c)
                    )

        return components

    def feature_signals(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        target_min: int = DEFAULT_SLEEP_TARGET_MIN,
    ) -> list[FeatureSignal]:
        """Per-feature anomaly inputs for the day (z, detector open, clear check).

        Uses the same windowed series and baseline computation as the recovery
        components, so a feature's anomaly z is exactly the z its component saw.
        A feature with an insufficient baseline (or no value today) yields no
        signal — the anomaly path has nothing to test for it that day.
        """
        window = baseline_window_days(local_health_day)
        span = [*window, local_health_day]
        signals: list[FeatureSignal] = []

        self._vital_signal(
            signals,
            code=AnomalyCode.LOW_HRV,
            series=self._features.daily_hrv(tenant_id=tenant_id, local_health_days=span),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.HRV,
            direction=Direction.HIGHER_BETTER,
            detect=detect_low_hrv,
            clears=clears_low_hrv,
        )
        self._vital_signal(
            signals,
            code=AnomalyCode.ELEVATED_RHR,
            series=self._features.daily_resting_hr(tenant_id=tenant_id, local_health_days=span),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.RHR,
            direction=Direction.LOWER_BETTER,
            detect=detect_elevated_rhr,
            clears=clears_elevated_rhr,
        )
        self._vital_signal(
            signals,
            code=AnomalyCode.DEVIANT_TEMPERATURE,
            series=self._features.daily_temperature_deviation(
                tenant_id=tenant_id, local_health_days=span
            ),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.TEMPERATURE,
            direction=Direction.DEVIATION_WORSE,
            detect=detect_deviant_temperature,
            clears=clears_deviant_temperature,
        )
        self._vital_signal(
            signals,
            code=AnomalyCode.ELEVATED_RESPIRATION,
            series=self._features.daily_respiratory_rate(
                tenant_id=tenant_id, local_health_days=span
            ),
            local_health_day=local_health_day,
            window=window,
            family=MetricFamily.RESPIRATORY,
            direction=Direction.ELEVATED_WORSE,
            detect=detect_elevated_respiration,
            clears=clears_elevated_respiration,
        )

        # Short sleep: shortfall vs target OR a low sleep-duration z, cleared
        # only when both the shortfall and the z recover.
        durations = self._features.daily_sleep_durations(
            tenant_id=tenant_id, local_health_days=span
        )
        today_duration = durations.get(local_health_day)
        if today_duration is not None:
            z = baseline_z_for(
                BaselineInput(
                    value=today_duration,
                    samples=[durations[d] for d in window if d in durations],
                    family=MetricFamily.SLEEP_DURATION,
                    direction=Direction.HIGHER_BETTER,
                )
            )
            if z is not None:
                shortfall = max(0.0, target_min - today_duration)
                signals.append(
                    FeatureSignal(
                        feature_code=AnomalyCode.SHORT_SLEEP.value,
                        open_today=detect_short_sleep(shortfall_min=shortfall, z_sleep_duration=z),
                        clear_today=clears_short_sleep(shortfall_min=shortfall, z_sleep_duration=z),
                        z_like=z,
                    )
                )

        return signals

    @staticmethod
    def _vital_signal(
        signals: list[FeatureSignal],
        *,
        code: AnomalyCode,
        series: dict[str, float],
        local_health_day: str,
        window: list[str],
        family: MetricFamily,
        direction: Direction,
        detect: Callable[[float], Anomaly | None],
        clears: Callable[[float], bool],
    ) -> None:
        """Append one scalar vital's anomaly signal when today's z is defined."""
        today = series.get(local_health_day)
        if today is None:
            return
        z = baseline_z_for(
            BaselineInput(
                value=today,
                samples=[series[d] for d in window if d in series],
                family=family,
                direction=direction,
            )
        )
        if z is None:
            return
        signals.append(
            FeatureSignal(
                feature_code=code.value,
                open_today=detect(z),
                clear_today=clears(z),
                z_like=z,
            )
        )

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


def _window_ending(target_day: str, days: int) -> list[str]:
    """The ``days`` calendar days ending on (and including) the target, oldest-first."""
    anchor = date.fromisoformat(target_day)
    span = [anchor - timedelta(days=offset) for offset in range(days)]
    return [day.isoformat() for day in reversed(span)]
