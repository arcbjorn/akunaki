"""Advance and persist anomaly intervals for a tenant's local day.

This is the stateful half of anomaly tracking: for each feature it reads the
prior interval state, advances the pure state machine with the day's detection
and clear inputs, and persists the transition (open / update / close). The pure
detectors and transition rules live in the domain; this service only wires them
to persistence.

Days do not arrive in order. A backfill normalizes a month of nights in
whatever order its jobs run, a vendor correction recomputes a week-old day, and
one day recomputes several times as its facts land (sleep in the morning,
activity at night). The state machine assumes one evaluation per day in
calendar order, so the tracker enforces that here:

- a day **older** than the last one the feature was advanced by is ignored —
  it cannot close an interval that opened after it (369 of some 400 rows on
  the production store ended before they started, 2026-11-08);
- the **same** day again is a re-read, not a second day: an interval opened
  on that day which no longer reads as anomalous is discarded (a morning with
  no steps yet is not a low-activity day), and a clear day counted on that
  day is counted once.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger("akunaki.anomalies")


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

    def last_tracked_day(self, *, tenant_id: str, feature_code: str) -> str | None:
        """The latest local day any of the feature's intervals was advanced by."""
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
        local_health_day: str,
        now: datetime,
    ) -> None:
        """Update the active interval's severity, clear-day run, and last day."""
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

    def discard_interval(self, *, tenant_id: str, feature_code: str) -> None:
        """Delete the active interval: its opening day no longer reads as anomalous."""
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
            frontier = (
                prior.tracked_through
                if prior is not None
                else self._store.last_tracked_day(
                    tenant_id=tenant_id, feature_code=signal.feature_code
                )
            )
            if frontier is not None and local_health_day < frontier:
                logger.info(
                    "anomaly tracking ignored an out-of-order day",
                    extra={
                        "tenant_id": tenant_id,
                        "feature_code": signal.feature_code,
                        "local_health_day": local_health_day,
                        "tracked_through": frontier,
                    },
                )
                continue
            if frontier is not None and local_health_day == frontier:
                prior = self._reread_day(
                    tenant_id=tenant_id,
                    signal=signal,
                    prior=prior,
                    local_health_day=local_health_day,
                    now=now,
                )
                if prior is None:
                    continue
            self._advance(
                tenant_id=tenant_id,
                signal=signal,
                prior=prior,
                local_health_day=local_health_day,
                now=now,
            )

    def _reread_day(
        self,
        *,
        tenant_id: str,
        signal: FeatureSignal,
        prior: AnomalyState | None,
        local_health_day: str,
        now: datetime,
    ) -> AnomalyState | None:
        """Undo what this day's earlier read did, or settle the re-read in place.

        Returns the state to advance from, or None when nothing more is to do.
        """
        if prior is None:
            # The interval closed on this very day. A same-day correction does
            # not reopen a close that two clear days earned.
            return None
        if prior.started_on == local_health_day:
            if signal.open_today is None:
                # Opened on a partial day that, complete, is not anomalous.
                self._store.discard_interval(tenant_id=tenant_id, feature_code=signal.feature_code)
                logger.info(
                    "anomaly retracted: its opening day no longer reads as anomalous",
                    extra={
                        "tenant_id": tenant_id,
                        "feature_code": signal.feature_code,
                        "local_health_day": local_health_day,
                    },
                )
            else:
                # Still anomalous; take the day's current severity, not the peak
                # of several partial reads.
                self._store.update_open_interval(
                    tenant_id=tenant_id,
                    feature_code=signal.feature_code,
                    severity=signal.open_today.severity,
                    consecutive_clear_days=0,
                    local_health_day=local_health_day,
                    now=now,
                )
            return None
        # Opened earlier; this day was already counted once. A clear run above
        # zero means the earlier read counted this day as clear, so take that
        # back. A run of zero means it did not (the run was reset) and the
        # pre-reset count is gone — starting from zero is the conservative
        # reading, delaying a clear by at most one day.
        return AnomalyState(
            is_open=True,
            severity=prior.severity,
            consecutive_clear_days=max(prior.consecutive_clear_days - 1, 0),
            started_on=prior.started_on,
            tracked_through=prior.tracked_through,
        )

    def _advance(
        self,
        *,
        tenant_id: str,
        signal: FeatureSignal,
        prior: AnomalyState | None,
        local_health_day: str,
        now: datetime,
    ) -> None:
        """Advance one feature by one day and persist the transition."""
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
            # Still open: refresh severity, the clear-day run, and the last day.
            assert transition.state.severity is not None
            self._store.update_open_interval(
                tenant_id=tenant_id,
                feature_code=signal.feature_code,
                severity=transition.state.severity,
                consecutive_clear_days=transition.state.consecutive_clear_days,
                local_health_day=local_health_day,
                now=now,
            )
        # else: stayed closed with nothing to persist.
