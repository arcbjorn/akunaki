"""Manual sync: what each connection status gets when a person asks for a sync.

``active`` and ``error`` connections get an incremental sync. A ``needs_reauth``
connection gets a reauth probe — the job that can lift the status without the
user — and ``revoked`` is refused. Fake ports keep the status matrix free of
the database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from akunaki.application.sync_request import (
    SyncKind,
    SyncRequestOutcome,
    SyncRequestRejection,
    SyncRequestService,
)
from akunaki.domain.connections import ConnectionStatus, LinkedConnection, Provider
from akunaki.domain.jobs import (
    INCREMENTAL_SYNC_JOB_TYPE,
    INITIAL_SYNC_JOB_TYPE,
    REAUTH_PROBE_JOB_TYPE,
    EnqueuedJob,
    JobRole,
)

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


class _FakeConnections:
    def __init__(self, status: ConnectionStatus) -> None:
        self._status = status

    def get_connection(self, *, connection_id: str) -> LinkedConnection | None:
        if connection_id != "conn-1":
            return None
        return LinkedConnection(
            connection_id="conn-1",
            tenant_id="tenant-1",
            provider=Provider.GOOGLE_HEALTH,
            status=self._status,
            scopes=(),
            external_user_id=None,
        )


class _FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, str | None]] = []

    def enqueue_job(
        self,
        *,
        job_id: str,
        tenant_id: str,
        job_type: str,
        payload_json: str,
        now: datetime,
        role: JobRole = JobRole.CORE,
        priority: int = 100,
        run_after: datetime | None = None,
        max_attempts: int = 5,
        idempotency_key: str | None = None,
    ) -> EnqueuedJob:
        self.enqueued.append((job_type, payload_json, idempotency_key))
        return EnqueuedJob(
            job_id=job_id, tenant_id=tenant_id, job_type=job_type, role=role, created=True
        )


def _request(
    status: ConnectionStatus,
) -> tuple[SyncRequestOutcome | SyncRequestRejection, _FakeJobs]:
    jobs = _FakeJobs()
    service = SyncRequestService(
        connections=_FakeConnections(status), jobs=jobs, new_id=lambda: "job-1"
    )
    outcome = service.request_sync(
        tenant_id="tenant-1", connection_id="conn-1", idempotency_key="k1", now=T0
    )
    return outcome, jobs


def test_an_errored_connection_gets_a_sync() -> None:
    """The sweep retries ``error`` by itself; refusing a person the same made no sense."""
    outcome, jobs = _request(ConnectionStatus.ERROR)

    assert isinstance(outcome, SyncRequestOutcome)
    assert outcome.kind is SyncKind.SYNC
    assert [kind for kind, _, _ in jobs.enqueued] == [INCREMENTAL_SYNC_JOB_TYPE]


def test_a_needs_reauth_connection_gets_a_probe_keyed_like_the_sweeps() -> None:
    """One job the person and the schedule share, so neither races the other."""
    outcome, jobs = _request(ConnectionStatus.NEEDS_REAUTH)

    assert isinstance(outcome, SyncRequestOutcome)
    assert outcome.kind is SyncKind.REAUTH_PROBE
    assert outcome.job_id == "job-1"
    job_type, payload_json, key = jobs.enqueued[0]
    assert job_type == REAUTH_PROBE_JOB_TYPE
    assert json.loads(payload_json) == {"connection_id": "conn-1"}
    assert key == "reauth_probe:conn-1"


def test_a_revoked_connection_is_refused() -> None:
    outcome, jobs = _request(ConnectionStatus.REVOKED)

    assert outcome is SyncRequestRejection.NOT_SYNCABLE
    assert jobs.enqueued == []


def test_an_active_connection_still_gets_a_plain_sync() -> None:
    outcome, jobs = _request(ConnectionStatus.ACTIVE)

    assert isinstance(outcome, SyncRequestOutcome)
    assert outcome.kind is SyncKind.SYNC
    job_type, _, key = jobs.enqueued[0]
    assert job_type == INCREMENTAL_SYNC_JOB_TYPE
    assert key == "manual_sync:conn-1:k1"


def test_a_backfill_queues_one_initial_sync_per_stream_with_the_lookback() -> None:
    jobs = _FakeJobs()
    service = SyncRequestService(
        connections=_FakeConnections(ConnectionStatus.ACTIVE), jobs=jobs, new_id=lambda: "job-1"
    )

    outcome = service.request_sync(
        tenant_id="tenant-1", connection_id="conn-1", idempotency_key="k1", now=T0, backfill_days=60
    )

    assert isinstance(outcome, SyncRequestOutcome)
    assert outcome.kind is SyncKind.BACKFILL
    assert outcome.created is True
    # Google Health has three streams; each gets its own job and key.
    assert {job_type for job_type, _, _ in jobs.enqueued} == {INITIAL_SYNC_JOB_TYPE}
    assert len(jobs.enqueued) == 3
    for _job_type, payload_json, key in jobs.enqueued:
        payload = json.loads(payload_json)
        assert payload["lookback_days"] == 60
        assert key == f"backfill:conn-1:{payload['stream']}:k1"


def test_a_backfill_on_a_revoked_connection_is_still_refused() -> None:
    jobs = _FakeJobs()
    service = SyncRequestService(
        connections=_FakeConnections(ConnectionStatus.REVOKED), jobs=jobs, new_id=lambda: "job-1"
    )

    outcome = service.request_sync(
        tenant_id="tenant-1", connection_id="conn-1", idempotency_key="k1", now=T0, backfill_days=60
    )

    assert outcome is SyncRequestRejection.NOT_SYNCABLE
    assert jobs.enqueued == []
