"""Advance and persist anomaly intervals for a tenant's local day.

This is the stateful half of anomaly tracking: for each feature it reads the
prior interval state, advances the pure state machine with the day's detection
and clear inputs, and persists the transition (open / update / close). The pure
detectors and transition rules live in the domain; this service only wires them
to persistence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from akunaki.domain.anomalies import (
    RULESET_VERSION,
    Anomaly,
    AnomalySeverity,
    AnomalyState,
    advance_anomaly,
)


@dataclass(frozen=True, slots=True)
class FeatureSignal:
    """One feature's per-day anomaly inputs.

    ``open_today`` is the detector's result (None when the open condition does
    not hold); ``clear_today`` is whether the direction-aware clear condition
    holds; ``z_like`` records the day's z for storage.
    """

    feature_code: str
    open_today: Anomaly | None
    clear_today: bool
    z_like: float | None


class AnomalyStateStore(Protocol):
    """Port: read and persist tracked anomaly intervals."""

    def current_state(self, *, tenant_id: str, feature_code: str) -> AnomalyState | None:
        """Return the active interval's state, or None."""
        ...

    def open_interval(
        self,
        *,
        anomaly_id: str,
        tenant_id: str,
        feature_code: str,
        severity: AnomalySeverity,
        z_like: float | None,
        formula_version: str,
        local_health_day: str,
        now: datetime,
    ) -> None:
        """Open a new active interval."""
        ...

    def update_open_interval(
        self,
        *,
        tenant_id: str,
        feature_code: str,
        severity: AnomalySeverity,
        consecutive_clear_days: int,
        now: datetime,
    ) -> None:
        """Update the active interval's severity and clear-day run."""
        ...

    def close_interval(
        self,
        *,
        tenant_id: str,
        feature_code: str,
        local_health_day: str,
        now: datetime,
    ) -> None:
        """Close the active interval."""
        ...


class AnomalyTracker:
    """Advance and persist a tenant's anomaly intervals for one day."""

    def __init__(
        self,
        *,
        store: AnomalyStateStore,
        new_id: Callable[[], str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._new_id = new_id
        self._clock = clock

    def track(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        signals: list[FeatureSignal],
    ) -> None:
        """Advance each feature's interval and persist the transition."""
        now = self._clock()
        for signal in signals:
            prior = self._store.current_state(tenant_id=tenant_id, feature_code=signal.feature_code)
            transition = advance_anomaly(
                prior=prior,
                open_today=signal.open_today,
                clear_today=signal.clear_today,
            )

            if transition.opened:
                assert transition.state.severity is not None
                self._store.open_interval(
                    anomaly_id=self._new_id(),
                    tenant_id=tenant_id,
                    feature_code=signal.feature_code,
                    severity=transition.state.severity,
                    z_like=signal.z_like,
                    formula_version=RULESET_VERSION,
                    local_health_day=local_health_day,
                    now=now,
                )
            elif transition.cleared:
                self._store.close_interval(
                    tenant_id=tenant_id,
                    feature_code=signal.feature_code,
                    local_health_day=local_health_day,
                    now=now,
                )
            elif transition.state.is_open:
                # Still open: refresh severity and the clear-day run.
                assert transition.state.severity is not None
                self._store.update_open_interval(
                    tenant_id=tenant_id,
                    feature_code=signal.feature_code,
                    severity=transition.state.severity,
                    consecutive_clear_days=transition.state.consecutive_clear_days,
                    now=now,
                )
            # else: stayed closed with nothing to persist.
