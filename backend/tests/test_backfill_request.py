"""The enqueue site for ``connection.initial_sync``.

The job type had a registered handler and nothing in ``src/`` that could reach
it, so a linked connection's first data waited on the reconcile sweep. These
cover the service that closes that gap: fan-out per stream, idempotency, and
the refusal to turn a wiring gap into a failed link.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count

import pytest

from akunaki.application.backfill_request import BackfillRequestService
from akunaki.application.sync_handlers import streams_for_provider
from akunaki.domain.jobs import INITIAL_SYNC_JOB_TYPE, EnqueuedJob, JobRole

T0 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


class _RecordingJobs:
    """Job port that records enqueues and deduplicates on the idempotency key."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._by_key: dict[str, str] = {}

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
        self.calls.append(
            {
                "job_id": job_id,
                "tenant_id": tenant_id,
                "job_type": job_type,
                "payload_json": payload_json,
                "idempotency_key": idempotency_key,
            }
        )
        existing = self._by_key.get(idempotency_key) if idempotency_key is not None else None
        if idempotency_key is not None and existing is None:
            self._by_key[idempotency_key] = job_id
        return EnqueuedJob(
            job_id=existing or job_id,
            tenant_id=tenant_id,
            job_type=job_type,
            role=role,
            created=existing is None,
        )


def _service(jobs: _RecordingJobs) -> BackfillRequestService:
    ids = count(1)
    return BackfillRequestService(jobs=jobs, new_id=lambda: f"job-{next(ids)}")


def test_one_job_per_stream_the_provider_serves() -> None:
    """Fan-out mirrors the reconcile sweep, for the same reason.

    Each stream keeps its own cursor and retry budget, so a stream that is
    failing (or that this account has no data for) must not stop the others.
    """
    jobs = _RecordingJobs()

    outcome = _service(jobs).request_backfill(
        tenant_id="tenant-1", connection_id="conn-1", provider="oura", now=T0
    )

    expected = {stream for stream, _schema in streams_for_provider("oura")}
    assert len(outcome.job_ids) == len(expected)
    assert outcome.created == len(expected)
    assert {call["job_type"] for call in jobs.calls} == {INITIAL_SYNC_JOB_TYPE}
    payloads = [json.loads(str(call["payload_json"])) for call in jobs.calls]
    assert {p["stream"] for p in payloads} == expected
    assert {p["connection_id"] for p in payloads} == {"conn-1"}


def test_the_key_is_per_connection_and_stream() -> None:
    """Keying on the connection alone would let one stream suppress the rest."""
    jobs = _RecordingJobs()

    _service(jobs).request_backfill(
        tenant_id="tenant-1", connection_id="conn-1", provider="oura", now=T0
    )

    keys = [call["idempotency_key"] for call in jobs.calls]
    assert len(set(keys)) == len(keys)
    assert all(str(key).startswith("initial_sync:conn-1:") for key in keys)


def test_a_repeated_request_does_not_stack_a_second_backfill() -> None:
    """Re-consenting to a linked connector must not double the vendor load."""
    jobs = _RecordingJobs()
    service = _service(jobs)

    first = service.request_backfill(
        tenant_id="tenant-1", connection_id="conn-1", provider="polar", now=T0
    )
    second = service.request_backfill(
        tenant_id="tenant-1", connection_id="conn-1", provider="polar", now=T0
    )

    assert first.created > 0
    assert second.created == 0
    # Same jobs reported back, so a caller cannot mistake dedupe for new work.
    assert first.job_ids == second.job_ids


def test_two_connections_do_not_share_a_backfill() -> None:
    """The key is per connection: a second connector is separate work."""
    jobs = _RecordingJobs()
    service = _service(jobs)

    first = service.request_backfill(
        tenant_id="tenant-1", connection_id="conn-1", provider="polar", now=T0
    )
    second = service.request_backfill(
        tenant_id="tenant-1", connection_id="conn-2", provider="polar", now=T0
    )

    assert second.created == first.created
    assert set(first.job_ids).isdisjoint(second.job_ids)


def test_an_unwired_provider_queues_nothing_rather_than_raising() -> None:
    """The link itself succeeded, so a wiring gap must not report it as broken.

    The reconcile sweep still reaches the connection once it is stale, so the
    backfill is delayed rather than lost.
    """
    jobs = _RecordingJobs()

    outcome = _service(jobs).request_backfill(
        tenant_id="tenant-1", connection_id="conn-1", provider="fitbit", now=T0
    )

    assert outcome == outcome.__class__(job_ids=(), created=0)
    assert jobs.calls == []


def test_a_job_store_failure_propagates_to_the_caller() -> None:
    """The *service* does not swallow: whether a failed link is fatal is the
    route's decision, and burying it here would leave no way to log it."""

    class _Broken(_RecordingJobs):
        def enqueue_job(self, **kwargs: object) -> EnqueuedJob:
            msg = "job store unavailable"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="job store unavailable"):
        _service(_Broken()).request_backfill(
            tenant_id="tenant-1", connection_id="conn-1", provider="oura", now=T0
        )
