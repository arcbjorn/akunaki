"""Worker runtime policy against an in-memory fake repository.

These tests exercise the loop's decisions (retry vs dead letter, lease loss,
leader gating, shutdown) without a database. Durable behavior itself is
covered by the job lifecycle and concurrency suites.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from akunaki.application.handlers import NOOP_JOB_TYPE, HandlerRegistry
from akunaki.application.worker_runtime import REAPER_LEASE_NAME, JobWorker, WorkerConfig
from akunaki.domain.jobs import (
    JobCandidate,
    JobClaim,
    JobFailureDisposition,
    JobFailureResult,
    JobRole,
    LeaderClaim,
    to_utc_rfc3339,
)
from akunaki.domain.retry import PermanentJobError, TransientJobError

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


class FakeRepository:
    """Minimal in-memory JobRepositoryPort double recording every call."""

    def __init__(
        self,
        *,
        claims: list[JobClaim] | None = None,
        heartbeat_alive: bool = True,
        complete_result: bool = True,
        leader_available: bool = True,
    ) -> None:
        self._claims = list(claims or [])
        self.heartbeat_alive = heartbeat_alive
        self.complete_result = complete_result
        self.leader_available = leader_available
        self.completed: list[str] = []
        self.failures: list[dict[str, object]] = []
        self.heartbeats: list[str] = []
        self.requeue_calls = 0
        self.dead_letter_calls = 0
        self.leader_acquires = 0
        self.leader_heartbeats = 0

    def claim_next(
        self,
        *,
        role: JobRole,
        owner: str,
        lease_ttl: timedelta,
        now: datetime,
        limit: int = 32,
    ) -> JobClaim | None:
        if not self._claims:
            return None
        return self._claims.pop(0)

    def heartbeat_job(
        self,
        *,
        job_id: str,
        owner: str,
        fence_token: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        self.heartbeats.append(job_id)
        return self.heartbeat_alive

    def complete_job(self, *, job_id: str, owner: str, fence_token: int, now: datetime) -> bool:
        if self.complete_result:
            self.completed.append(job_id)
        return self.complete_result

    def fail_job(
        self,
        *,
        job_id: str,
        owner: str,
        fence_token: int,
        retryable: bool,
        retry_delay: timedelta,
        error_class: str,
        redacted_error_message: str | None,
        now: datetime,
    ) -> JobFailureResult | None:
        self.failures.append(
            {
                "job_id": job_id,
                "retryable": retryable,
                "retry_delay": retry_delay,
                "error_class": error_class,
                "message": redacted_error_message,
            }
        )
        disposition = (
            JobFailureDisposition.RETRY_SCHEDULED
            if retryable
            else JobFailureDisposition.DEAD_LETTERED
        )
        return JobFailureResult(
            disposition=disposition,
            job_id=job_id,
            attempt_number=1,
            fence_token=fence_token,
        )

    def requeue_expired_leases(self, *, now: datetime) -> int:
        self.requeue_calls += 1
        return 2

    def dead_letter_expired_jobs(self, *, now: datetime) -> int:
        self.dead_letter_calls += 1
        return 1

    def try_acquire_leader(
        self, *, lease_name: str, owner: str, lease_ttl: timedelta, now: datetime
    ) -> LeaderClaim | None:
        self.leader_acquires += 1
        if not self.leader_available:
            return None
        return LeaderClaim(
            lease_name=lease_name,
            owner=owner,
            fence_token=1,
            leased_until=to_utc_rfc3339(now + lease_ttl),
        )

    def heartbeat_leader(
        self,
        *,
        lease_name: str,
        owner: str,
        fence_token: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        self.leader_heartbeats += 1
        return self.leader_available

    # Unused by the runtime but required by the port surface.
    def discover_due_candidates(
        self, *, role: JobRole, now: datetime, limit: int
    ) -> Sequence[JobCandidate]:
        return []

    def try_claim_job(
        self, candidate: JobCandidate, *, owner: str, lease_ttl: timedelta, now: datetime
    ) -> JobClaim | None:
        return None

    def has_valid_leadership(
        self, *, lease_name: str, owner: str, fence_token: int, now: datetime
    ) -> bool:
        return self.leader_available

    def has_valid_job_lease(
        self, *, job_id: str, owner: str, fence_token: int, now: datetime
    ) -> bool:
        return True


def make_claim(job_type: str = NOOP_JOB_TYPE, *, attempts: int = 1) -> JobClaim:
    return JobClaim(
        job_id="job-1",
        tenant_id="tenant-1",
        role=JobRole.CORE,
        job_type=job_type,
        owner="worker-1",
        fence_token=1,
        leased_until=to_utc_rfc3339(NOW + timedelta(seconds=30)),
        attempts=attempts,
        max_attempts=5,
        payload_json="{}",
    )


def make_worker(
    repository: FakeRepository,
    *,
    registry: HandlerRegistry | None = None,
    config: WorkerConfig | None = None,
) -> JobWorker:
    return JobWorker(
        repository,
        owner="worker-1",
        config=config or WorkerConfig(),
        registry=registry,
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )


def test_successful_job_completes_and_counts() -> None:
    repo = FakeRepository(claims=[make_claim()])
    worker = make_worker(repo)

    assert worker.run_once() is True
    assert repo.completed == ["job-1"]
    assert worker.stats.succeeded == 1
    assert worker.stats.claimed == 1


def test_empty_queue_reports_no_work() -> None:
    repo = FakeRepository()
    worker = make_worker(repo)

    assert worker.run_once() is False
    assert worker.stats.claimed == 0


def test_transient_failure_schedules_retry_with_backoff() -> None:
    registry = HandlerRegistry({"boom": _raise(TransientJobError("vendor 503"))})
    repo = FakeRepository(claims=[make_claim("boom", attempts=3)])
    worker = make_worker(repo, registry=registry)

    worker.run_once()

    assert worker.stats.retried == 1
    assert worker.stats.dead_lettered == 0
    failure = repo.failures[0]
    assert failure["retryable"] is True
    # Third attempt with zero jitter on the default 1s base: 1 * 2**2.
    assert failure["retry_delay"] == timedelta(seconds=4)
    assert failure["error_class"] == "TransientJobError"


def test_permanent_failure_dead_letters_immediately() -> None:
    registry = HandlerRegistry({"bad": _raise(PermanentJobError("unsupported payload"))})
    repo = FakeRepository(claims=[make_claim("bad")])
    worker = make_worker(repo, registry=registry)

    worker.run_once()

    assert worker.stats.dead_lettered == 1
    assert worker.stats.retried == 0
    assert repo.failures[0]["retryable"] is False


def test_unregistered_job_type_dead_letters_without_burning_retries() -> None:
    repo = FakeRepository(claims=[make_claim("connector.unknown")])
    worker = make_worker(repo, registry=HandlerRegistry())

    worker.run_once()

    assert worker.stats.unhandled_type == 1
    assert worker.stats.dead_lettered == 1
    assert repo.failures[0]["retryable"] is False
    assert repo.failures[0]["error_class"] == "UnregisteredJobType"


def test_rejected_completion_records_lease_loss_not_success() -> None:
    repo = FakeRepository(claims=[make_claim()], complete_result=False)
    worker = make_worker(repo)

    worker.run_once()

    assert worker.stats.succeeded == 0
    assert worker.stats.lease_lost == 1
    assert repo.completed == []


def test_lease_lost_during_execution_skips_completion() -> None:
    # A handler that outlives its lease must not report success: the heartbeat
    # rejection is observed and completion is never attempted.
    repo = FakeRepository(claims=[make_claim()], heartbeat_alive=False)
    config = WorkerConfig(
        lease_ttl=timedelta(seconds=2),
        heartbeat_interval=timedelta(milliseconds=10),
    )
    slow = HandlerRegistry({NOOP_JOB_TYPE: lambda _claim: __import__("time").sleep(0.2)})
    worker = make_worker(repo, registry=slow, config=config)

    worker.run_once()

    assert repo.heartbeats  # heartbeat actually ran
    assert worker.stats.lease_lost == 1
    assert worker.stats.succeeded == 0
    assert repo.completed == []


def test_fence_rejected_failure_records_lease_loss() -> None:
    class RejectingRepo(FakeRepository):
        def fail_job(self, **kwargs: object) -> JobFailureResult | None:
            return None

    repo = RejectingRepo(claims=[make_claim("boom")])
    registry = HandlerRegistry({"boom": _raise(TransientJobError("x"))})
    worker = make_worker(repo, registry=registry)

    worker.run_once()

    assert worker.stats.lease_lost == 1
    assert worker.stats.retried == 0


def test_leader_runs_reaper_duties() -> None:
    repo = FakeRepository()
    worker = make_worker(repo)

    worker.run_once()

    assert repo.leader_acquires == 1
    assert repo.requeue_calls == 1
    assert repo.dead_letter_calls == 1
    assert worker.stats.requeued_expired == 2
    assert worker.stats.dead_lettered_expired == 1


def test_non_leader_never_reaps() -> None:
    # A passive standby must not requeue or dead-letter behind the active
    # worker's back.
    repo = FakeRepository(leader_available=False)
    worker = make_worker(repo)

    worker.run_once()

    assert repo.requeue_calls == 0
    assert repo.dead_letter_calls == 0


def test_reaper_respects_its_interval() -> None:
    repo = FakeRepository()
    clock = _AdvancingClock(NOW)
    worker = JobWorker(
        repo,
        owner="worker-1",
        config=WorkerConfig(reaper_interval=timedelta(seconds=15)),
        clock=clock,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )

    worker.run_once()
    worker.run_once()  # immediately after: interval not elapsed
    assert repo.requeue_calls == 1

    clock.advance(timedelta(seconds=20))
    worker.run_once()
    assert repo.requeue_calls == 2


def test_run_forever_exits_on_stop_request() -> None:
    repo = FakeRepository(claims=[make_claim()])
    stop = threading.Event()
    worker = JobWorker(
        repo,
        owner="worker-1",
        stop_event=stop,
        clock=lambda: NOW,
        sleep=lambda _s: stop.set(),  # first idle poll requests shutdown
        jitter=lambda: 0.0,
    )

    stats = worker.run_forever()

    assert stats.succeeded == 1
    assert stop.is_set()


def test_invalid_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="heartbeat_interval must be shorter"):
        WorkerConfig(lease_ttl=timedelta(seconds=5), heartbeat_interval=timedelta(seconds=5))
    with pytest.raises(ValueError, match="lease_ttl must be at least 1 second"):
        WorkerConfig(lease_ttl=timedelta(milliseconds=500))
    with pytest.raises(ValueError, match="claim_limit must be >= 1"):
        WorkerConfig(claim_limit=0)


def test_empty_owner_is_rejected() -> None:
    with pytest.raises(ValueError, match="owner must be a non-empty string"):
        JobWorker(FakeRepository(), owner="   ")


def test_reaper_lease_name_is_stable() -> None:
    # Standby promotion depends on both workers contending for the same name.
    assert REAPER_LEASE_NAME == "core-reaper"


class _AdvancingClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _raise(exc: Exception):  # type: ignore[no-untyped-def]
    def handler(_claim: JobClaim) -> None:
        raise exc

    return handler
