"""Tests for the pure periodic-schedule decision."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from akunaki.domain.schedule import ScheduleSpec, due_schedules

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _spec(key: str = "reconcile", minutes: int = 30) -> ScheduleSpec:
    return ScheduleSpec(
        job_type="connection.reconcile_sweep",
        interval=timedelta(minutes=minutes),
        tenant_id="tenant-1",
        idempotency_key=key,
    )


def test_never_fired_is_due_immediately() -> None:
    due = due_schedules([_spec()], last_fired={}, now=T0)
    assert len(due) == 1


def test_within_interval_is_not_due() -> None:
    spec = _spec(minutes=30)
    due = due_schedules(
        [spec], last_fired={spec.idempotency_key: T0 - timedelta(minutes=10)}, now=T0
    )
    assert due == []


def test_at_or_past_interval_is_due() -> None:
    spec = _spec(minutes=30)
    due = due_schedules(
        [spec], last_fired={spec.idempotency_key: T0 - timedelta(minutes=30)}, now=T0
    )
    assert due == [spec]


def test_mixed_specs_return_only_the_due_ones() -> None:
    a = _spec(key="a", minutes=10)
    b = _spec(key="b", minutes=60)
    last = {"a": T0 - timedelta(minutes=15), "b": T0 - timedelta(minutes=15)}
    due = due_schedules([a, b], last_fired=last, now=T0)
    assert due == [a]  # a is overdue; b is not


def test_invalid_spec_is_rejected() -> None:
    with pytest.raises(ValueError, match="interval must be positive"):
        ScheduleSpec(job_type="x", interval=timedelta(0), tenant_id="t", idempotency_key="k")
    with pytest.raises(ValueError, match="must be non-empty"):
        ScheduleSpec(job_type="", interval=timedelta(minutes=1), tenant_id="t", idempotency_key="k")
