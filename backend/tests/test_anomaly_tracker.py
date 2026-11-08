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
        self.discarded: list[str] = []
        # The last day each feature was advanced by, surviving a close.
        self.frontier: dict[tuple[str, str], str] = {}

    def current_state(self, *, tenant_id: str, feature_code: str) -> AnomalyState | None:
        return self.states.get((tenant_id, feature_code))

    def last_tracked_day(self, *, tenant_id: str, feature_code: str) -> str | None:
        return self.frontier.get((tenant_id, feature_code))

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
            is_open=True,
            severity=severity,
            consecutive_clear_days=0,
            started_on=local_health_day,
            tracked_through=local_health_day,
        )
        self.frontier[(tenant_id, feature_code)] = local_health_day
        self.opened.append(anomaly_id)

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
        prior = self.states[(tenant_id, feature_code)]
        self.states[(tenant_id, feature_code)] = AnomalyState(
            is_open=True,
            severity=severity,
            consecutive_clear_days=consecutive_clear_days,
            started_on=prior.started_on,
            tracked_through=local_health_day,
        )
        self.frontier[(tenant_id, feature_code)] = local_health_day

    def close_interval(
        self,
        *,
        tenant_id: str,
        feature_code: str,
        local_health_day: str,
        now: datetime,
    ) -> None:
        self.states.pop((tenant_id, feature_code), None)
        self.frontier[(tenant_id, feature_code)] = local_health_day
        self.closed.append(feature_code)

    def discard_interval(self, *, tenant_id: str, feature_code: str, now: datetime) -> None:
        self.states.pop((tenant_id, feature_code), None)
        self.discarded.append(feature_code)


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


# ---------------------------------------------------------------------------
# Day ordering: recompute jobs do not arrive in calendar order
# ---------------------------------------------------------------------------


def _signal(open_today: Anomaly | None, *, clear: bool) -> list[FeatureSignal]:
    return [FeatureSignal("low_hrv", open_today=open_today, clear_today=clear, z_like=None)]


def test_an_older_day_cannot_close_an_interval_that_opened_after_it() -> None:
    """A backfill processing days out of order must not end an interval before it began.

    On the production store 369 rows ended before they started: an interval
    opened on October 5 was "cleared" by September 29 and 23 reading as normal.
    """
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(_MOD, clear=False)
    )

    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-09-29", signals=_signal(None, clear=True)
    )
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-09-23", signals=_signal(None, clear=True)
    )

    state = store.states[("tenant-1", "low_hrv")]
    assert state.is_open is True
    assert state.consecutive_clear_days == 0
    assert store.closed == []


def test_an_older_day_does_not_reopen_after_a_close() -> None:
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-01", signals=_signal(_MOD, clear=False)
    )
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-02", signals=_signal(None, clear=True)
    )
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-03", signals=_signal(None, clear=True)
    )
    assert store.closed == ["low_hrv"]

    # A late recompute of a day inside the closed interval.
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-02", signals=_signal(_MOD, clear=False)
    )

    assert store.opened == ["a-1"]
    assert store.states == {}


def test_a_same_day_reread_retracts_an_open_the_full_day_does_not_support() -> None:
    """A morning with no steps yet is not a low-activity day.

    The first recompute of a day runs when its first fact lands; the detector
    then sees a partial day. When the day is re-read complete and no longer
    anomalous, the interval it opened is discarded rather than closed, so no
    zero-length anomaly reaches the user's history.
    """
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(_MOD, clear=False)
    )

    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(None, clear=True)
    )

    assert store.discarded == ["low_hrv"]
    assert store.closed == []
    assert store.states == {}


def test_a_same_day_reread_refreshes_severity_instead_of_opening_twice() -> None:
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(_MOD, clear=False)
    )
    high = Anomaly(code=AnomalyCode.LOW_HRV, severity=AnomalySeverity.HIGH)

    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(high, clear=False)
    )

    assert store.opened == ["a-1"]
    assert store.states[("tenant-1", "low_hrv")].severity is AnomalySeverity.HIGH


def test_a_same_day_reread_counts_a_clear_day_once() -> None:
    """Several recomputes of one clear day must not add up to the two-day clear."""
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(_MOD, clear=False)
    )
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-06", signals=_signal(None, clear=True)
    )
    assert store.states[("tenant-1", "low_hrv")].consecutive_clear_days == 1

    # The same day again, as its evening facts land.
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-06", signals=_signal(None, clear=True)
    )

    assert store.states[("tenant-1", "low_hrv")].consecutive_clear_days == 1
    assert store.closed == []
    # The genuinely next clear day then closes it.
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-07", signals=_signal(None, clear=True)
    )
    assert store.closed == ["low_hrv"]


def test_a_same_day_reread_can_take_back_a_clear_day() -> None:
    """A day first read as clear, then re-read as not clear, counts as not clear."""
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(_MOD, clear=False)
    )
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-06", signals=_signal(None, clear=True)
    )

    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-06", signals=_signal(None, clear=False)
    )

    assert store.states[("tenant-1", "low_hrv")].consecutive_clear_days == 0


def test_a_close_is_not_reopened_by_a_same_day_correction() -> None:
    store = _FakeStore()
    tracker = _tracker(store)
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-05", signals=_signal(_MOD, clear=False)
    )
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-06", signals=_signal(None, clear=True)
    )
    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-07", signals=_signal(None, clear=True)
    )
    assert store.closed == ["low_hrv"]

    tracker.track(
        tenant_id="tenant-1", local_health_day="2026-10-07", signals=_signal(_MOD, clear=False)
    )

    assert store.opened == ["a-1"]
    assert store.states == {}
