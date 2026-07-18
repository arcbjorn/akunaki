"""End-to-end worker runtime against the real repository and file-backed libSQL.

The runtime policy tests use a fake repository; these prove the same loop
drives the durable lifecycle correctly through real SQL: success, retry with a
real re-claim, dead lettering, and leader-gated expiry reaping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Job, JobAttempt, JobDeadLetter, Tenant
from akunaki.application.handlers import NOOP_JOB_TYPE, HandlerRegistry
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.domain.jobs import (
    JobAttemptStatus,
    JobClaim,
    JobRole,
    JobStatus,
    to_utc_rfc3339,
)
from akunaki.domain.retry import PermanentJobError, TransientJobError

T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def _seed(
    factory: sessionmaker[Session],
    *,
    job_id: str = "job-1",
    job_type: str = NOOP_JOB_TYPE,
    max_attempts: int = 3,
    status: str = JobStatus.READY.value,
    with_tenant: bool = True,
) -> None:
    now_s = to_utc_rfc3339(T0)
    with factory() as session, session.begin():
        if with_tenant:
            session.add(
                Tenant(
                    id="tenant-1",
                    created_at=now_s,
                    status="active",
                    primary_timezone="UTC",
                    display_name="Test",
                )
            )
        session.add(
            Job(
                id=job_id,
                tenant_id="tenant-1",
                role=JobRole.CORE.value,
                status=status,
                payload_json='{"kind":"ping"}',
                priority=100,
                run_after=now_s,
                attempts=0,
                max_attempts=max_attempts,
                idempotency_key=job_id,
                fence_token=0,
                created_at=now_s,
                updated_at=now_s,
                job_type=job_type,
            )
        )


def _job(factory: sessionmaker[Session], job_id: str = "job-1") -> Job:
    with factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def _attempts(factory: sessionmaker[Session], job_id: str = "job-1") -> list[JobAttempt]:
    with factory() as session:
        rows = list(
            session.scalars(
                select(JobAttempt)
                .where(JobAttempt.job_id == job_id)
                .order_by(JobAttempt.attempt_number)
            ).all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _worker(
    factory: sessionmaker[Session],
    *,
    registry: HandlerRegistry | None = None,
    now: datetime = T0,
    config: WorkerConfig | None = None,
) -> JobWorker:
    return JobWorker(
        JobRepository(factory),
        owner="worker-e2e",
        config=config or WorkerConfig(),
        registry=registry,
        clock=lambda: now,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )


def test_worker_executes_job_and_persists_success(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory)
    executed: list[JobClaim] = []
    registry = HandlerRegistry({NOOP_JOB_TYPE: executed.append})

    worker = _worker(session_factory, registry=registry)
    assert worker.run_once() is True

    # Handler saw the real claim, including payload and fencing metadata.
    assert len(executed) == 1
    assert executed[0].job_id == "job-1"
    assert executed[0].payload_json == '{"kind":"ping"}'

    job = _job(session_factory)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.attempts == 1

    attempts = _attempts(session_factory)
    assert len(attempts) == 1
    assert attempts[0].status == JobAttemptStatus.SUCCEEDED.value
    assert attempts[0].finished_at is not None


def test_transient_failure_persists_retry_then_reclaims_and_succeeds(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory)
    calls: list[int] = []

    def flaky(_claim: JobClaim) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise TransientJobError("vendor timeout")

    registry = HandlerRegistry({NOOP_JOB_TYPE: flaky})

    # First pass fails and schedules a retry.
    _worker(session_factory, registry=registry).run_once()
    job = _job(session_factory)
    assert job.status == JobStatus.READY.value
    assert job.attempts == 1
    assert job.last_error_class == "TransientJobError"

    # Second pass runs after the scheduled delay and succeeds, proving the
    # retry is genuinely re-claimable rather than merely recorded.
    later = T0 + timedelta(seconds=60)
    _worker(session_factory, registry=registry, now=later).run_once()

    job = _job(session_factory)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.attempts == 2
    assert len(calls) == 2

    attempts = _attempts(session_factory)
    assert [a.status for a in attempts] == [
        JobAttemptStatus.RETRY_SCHEDULED.value,
        JobAttemptStatus.SUCCEEDED.value,
    ]


def test_permanent_failure_dead_letters_on_first_attempt(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory, max_attempts=5)
    registry = HandlerRegistry({NOOP_JOB_TYPE: _raiser(PermanentJobError("unsupported payload"))})

    _worker(session_factory, registry=registry).run_once()

    job = _job(session_factory)
    # Attempts remain below max: permanence, not exhaustion, ended this job.
    assert job.status == JobStatus.DEAD_LETTER.value
    assert job.attempts == 1
    assert job.max_attempts == 5

    with session_factory() as session:
        dead = session.get(JobDeadLetter, "job-1")
        assert dead is not None
        assert dead.error_class == "PermanentJobError"


def test_retries_exhaust_into_dead_letter(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory, max_attempts=2)
    registry = HandlerRegistry({NOOP_JOB_TYPE: _raiser(TransientJobError("always down"))})

    now = T0
    for _ in range(2):
        _worker(session_factory, registry=registry, now=now).run_once()
        now += timedelta(minutes=5)

    job = _job(session_factory)
    assert job.status == JobStatus.DEAD_LETTER.value
    assert job.attempts == 2

    attempts = _attempts(session_factory)
    assert [a.status for a in attempts] == [
        JobAttemptStatus.RETRY_SCHEDULED.value,
        JobAttemptStatus.DEAD_LETTER.value,
    ]


def test_unregistered_job_type_dead_letters_through_real_lifecycle(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory, job_type="connector.oura.sync", max_attempts=5)

    worker = _worker(session_factory, registry=HandlerRegistry())
    worker.run_once()

    job = _job(session_factory)
    assert job.status == JobStatus.DEAD_LETTER.value
    assert job.last_error_class == "UnregisteredJobType"


def test_leader_reaper_requeues_expired_lease_from_a_crashed_worker(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory, max_attempts=3)
    repository = JobRepository(session_factory)

    # Simulate a worker that claimed the job then died without settling it.
    crashed = repository.claim_next(
        role=JobRole.CORE,
        owner="worker-crashed",
        lease_ttl=timedelta(seconds=30),
        now=T0,
    )
    assert crashed is not None
    assert _job(session_factory).status == JobStatus.LEASED.value

    # A live worker ticks well after that lease expired; as leader it reaps.
    after_expiry = T0 + timedelta(minutes=5)
    executed: list[JobClaim] = []
    worker = _worker(
        session_factory,
        registry=HandlerRegistry({NOOP_JOB_TYPE: executed.append}),
        now=after_expiry,
    )
    worker.run_once()

    assert worker.stats.requeued_expired == 1
    # Requeued and then claimed and completed in the same tick.
    assert len(executed) == 1
    job = _job(session_factory)
    assert job.status == JobStatus.SUCCEEDED.value
    # The crashed attempt's fence is superseded, so its owner cannot interfere.
    assert job.fence_token > crashed.fence_token


def test_empty_queue_leaves_no_durable_side_effects(
    session_factory: sessionmaker[Session],
) -> None:
    worker = _worker(session_factory)
    assert worker.run_once() is False
    assert worker.stats.claimed == 0
    assert _attempts(session_factory) == []


def _raiser(exc: Exception):  # type: ignore[no-untyped-def]
    def handler(_claim: JobClaim) -> None:
        raise exc

    return handler
