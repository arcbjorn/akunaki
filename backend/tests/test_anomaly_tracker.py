"""Tests for the anomaly tracker (state machine + persistence wiring)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from akunaki.application.anomaly_tracker import AnomalyTracker, FeatureSignal
from akunaki.domain.anomalies import Anomaly, AnomalyCode, AnomalySeverity, AnomalyState

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
_MOD = Anomaly(code=AnomalyCode.LOW_HRV, severity=AnomalySeverity.MODERATE)


class _FakeStore:
    """An in-memory anomaly store keyed by (tenant, feature)."""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], AnomalyState] = {}
        self.opened: list[str] = []
        self.closed: list[str] = []

    def current_state(self, *, tenant_id: str, feature_code: str) -> AnomalyState | None:
        return self.states.get((tenant_id, feature_code))

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
        self.states[(tenant_id, feature_code)] = AnomalyState(
            is_open=True, severity=severity, consecutive_clear_days=0
        )
        self.opened.append(anomaly_id)

    def update_open_interval(
        self,
        *,
        tenant_id: str,
        feature_code: str,
        severity: AnomalySeverity,
        consecutive_clear_days: int,
        now: datetime,
    ) -> None:
        self.states[(tenant_id, feature_code)] = AnomalyState(
            is_open=True,
            severity=severity,
            consecutive_clear_days=consecutive_clear_days,
        )

    def close_interval(
        self,
        *,
        tenant_id: str,
        feature_code: str,
        local_health_day: str,
        now: datetime,
    ) -> None:
        self.states.pop((tenant_id, feature_code), None)
        self.closed.append(feature_code)


def _tracker(store: _FakeStore) -> AnomalyTracker:
    ids = itertools.count(1)
    return AnomalyTracker(store=store, new_id=lambda: f"a-{next(ids)}", clock=lambda: T0)


def test_opens_a_new_interval() -> None:
    store = _FakeStore()
    _tracker(store).track(
        tenant_id="tenant-1",
        local_health_day="2026-07-22",
        signals=[FeatureSignal("low_hrv", open_today=_MOD, clear_today=False, z_like=-2.7)],
    )
    assert store.opened == ["a-1"]
    assert store.states[("tenant-1", "low_hrv")].is_open is True


def test_clears_after_two_consecutive_days() -> None:
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1",
        local_health_day="2026-07-22",
        signals=[FeatureSignal("low_hrv", open_today=_MOD, clear_today=False, z_like=-2.7)],
    )
    # Day 1 clear: still open, run == 1.
    tracker.track(
        tenant_id="tenant-1",
        local_health_day="2026-07-23",
        signals=[FeatureSignal("low_hrv", open_today=None, clear_today=True, z_like=-1.0)],
    )
    assert store.states[("tenant-1", "low_hrv")].consecutive_clear_days == 1
    # Day 2 clear: closes.
    tracker.track(
        tenant_id="tenant-1",
        local_health_day="2026-07-24",
        signals=[FeatureSignal("low_hrv", open_today=None, clear_today=True, z_like=-0.5)],
    )
    assert store.closed == ["low_hrv"]
    assert ("tenant-1", "low_hrv") not in store.states


def test_nothing_persisted_when_closed_and_no_open() -> None:
    store = _FakeStore()
    _tracker(store).track(
        tenant_id="tenant-1",
        local_health_day="2026-07-22",
        signals=[FeatureSignal("low_hrv", open_today=None, clear_today=True, z_like=0.0)],
    )
    assert store.opened == []
    assert store.closed == []
    assert store.states == {}
