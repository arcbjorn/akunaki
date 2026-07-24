"""Reconcile sweep handler: enqueue incremental syncs for stale connections.

Uses fake ports so the sweep logic (cutoff, idempotency key, per-connection
enqueue) is tested in isolation from the DB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from akunaki.application.sync_handlers import (
    INCREMENTAL_SYNC_JOB_TYPE,
    RECONCILE_SWEEP_JOB_TYPE,
    ReconcileSweepHandler,
)
from akunaki.domain.jobs import JobClaim, JobRole, to_utc_rfc3339

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


@dataclass
class _Enqueued:
    job_type: str
    tenant_id: str
    payload_json: str
    idempotency_key: str | None
    created: bool = True


class _FakeJobs:
    def __init__(self, *, existing_keys: set[str] | None = None) -> None:
        self.calls: list[_Enqueued] = []
        self._existing = existing_keys or set()

    def enqueue_job(
        self,
        *,
        job_id: str,
        tenant_id: str,
        job_type: str,
        payload_json: str,
        now: datetime,
        idempotency_key: str | None = None,
        **kwargs: object,
    ) -> _Enqueued:
        # An already-present key returns created=False and inserts nothing.
        created = idempotency_key not in self._existing
        record = _Enqueued(
            job_type=job_type,
            tenant_id=tenant_id,
            payload_json=payload_json,
            idempotency_key=idempotency_key,
            created=created,
        )
        self.calls.append(record)
        return record


class _FakeConnections:
    def __init__(self, stale: list[tuple[str, str]]) -> None:
        self._stale = stale
        self.cutoff_seen: str | None = None

    def stale_connections(self, *, cutoff: str, limit: int = 100) -> list[tuple[str, str]]:
        self.cutoff_seen = cutoff
        return self._stale[:limit]


def _claim() -> JobClaim:
    return JobClaim(
        job_id="sweep-1",
        tenant_id="tenant-1",
        role=JobRole.CORE,
        job_type=RECONCILE_SWEEP_JOB_TYPE,
        owner="worker-1",
        fence_token=1,
        leased_until="2026-07-24T13:00:00Z",
        attempts=1,
        max_attempts=5,
        payload_json="{}",
    )


def test_enqueues_one_incremental_sync_per_stale_connection() -> None:
    jobs = _FakeJobs()
    connections = _FakeConnections([("conn-a", "tenant-1"), ("conn-b", "tenant-2")])
    handler = ReconcileSweepHandler(
        connections=connections,
        jobs=jobs,
        new_id=lambda: "id",
        staleness=timedelta(hours=6),
        clock=lambda: T0,
    )

    handler(_claim())

    assert len(jobs.calls) == 2
    assert {c.job_type for c in jobs.calls} == {INCREMENTAL_SYNC_JOB_TYPE}
    # Each carries its own connection and tenant, keyed for idempotence.
    by_conn = {json.loads(c.payload_json)["connection_id"]: c for c in jobs.calls}
    assert by_conn["conn-a"].tenant_id == "tenant-1"
    assert by_conn["conn-a"].idempotency_key == "reconcile:conn-a"
    assert by_conn["conn-b"].tenant_id == "tenant-2"


def test_cutoff_is_now_minus_staleness() -> None:
    jobs = _FakeJobs()
    connections = _FakeConnections([])
    handler = ReconcileSweepHandler(
        connections=connections,
        jobs=jobs,
        new_id=lambda: "id",
        staleness=timedelta(hours=6),
        clock=lambda: T0,
    )
    handler(_claim())
    assert connections.cutoff_seen == to_utc_rfc3339(T0 - timedelta(hours=6))


def test_no_stale_connections_enqueues_nothing() -> None:
    jobs = _FakeJobs()
    handler = ReconcileSweepHandler(
        connections=_FakeConnections([]),
        jobs=jobs,
        new_id=lambda: "id",
        clock=lambda: T0,
    )
    handler(_claim())
    assert jobs.calls == []


def test_already_queued_connection_is_not_double_scheduled() -> None:
    # The idempotency key already exists (a webhook queued it): enqueue is a
    # no-op (created=False), so the sweep does not pile on.
    jobs = _FakeJobs(existing_keys={"reconcile:conn-a"})
    handler = ReconcileSweepHandler(
        connections=_FakeConnections([("conn-a", "tenant-1")]),
        jobs=jobs,
        new_id=lambda: "id",
        clock=lambda: T0,
    )
    handler(_claim())
    # The call is made (idempotent enqueue), but nothing new was created.
    assert len(jobs.calls) == 1
    assert jobs.calls[0].created is False
