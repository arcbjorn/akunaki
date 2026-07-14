"""File-backed integration tests for the durable job lifecycle."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Job, JobAttempt, JobDeadLetter, JobLease, Tenant
from akunaki.config import Settings
from akunaki.domain.jobs import (
    JobAttemptStatus,
    JobClaim,
    JobFailureDisposition,
    JobRole,
    JobStatus,
    to_utc_rfc3339,
)

T0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
LEASE_TTL = timedelta(seconds=30)
FAIL_AT = T0 + timedelta(seconds=1)
WORKER_LEASE_EXPIRED = "worker_lease_expired"


class _PersistedJobState(TypedDict):
    status: str
    attempts: int
    max_attempts: int
    fence_token: int
    run_after: str
    updated_at: str
    last_error_class: str | None


class _PersistedLeaseState(TypedDict):
    owner: str
    leased_until: str
    fence_token: int


class _PersistedAttemptState(TypedDict):
    id: str
    attempt_number: int
    fence_token: int
    owner: str
    status: str
    error_class: str | None
    message: str | None
    started_at: str
    finished_at: str | None


class _PersistedDeadLetterState(TypedDict):
    tenant_id: str
    attempt_number: int
    fence_token: int
    error_class: str
    message: str | None
    dead_lettered_at: str


class _PersistedState(TypedDict):
    job: _PersistedJobState
    lease: _PersistedLeaseState | None
    attempts: list[_PersistedAttemptState]
    dead_letter: _PersistedDeadLetterState | None


def _seed_job(
    factory: sessionmaker[Session],
    *,
    job_id: str = "job-1",
    max_attempts: int = 5,
) -> None:
    now_s = to_utc_rfc3339(T0)
    with factory() as session, session.begin():
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
                status=JobStatus.READY.value,
                payload_json='{"kind":"ping"}',
                priority=100,
                run_after=now_s,
                attempts=0,
                max_attempts=max_attempts,
                idempotency_key=job_id,
                fence_token=0,
                created_at=now_s,
                updated_at=now_s,
                job_type="system.noop",
            )
        )


def _claim(
    repository: JobRepository,
    *,
    owner: str = "worker-a",
    now: datetime = T0,
) -> JobClaim:
    claim = repository.claim_next(
        role=JobRole.CORE,
        owner=owner,
        lease_ttl=LEASE_TTL,
        now=now,
    )
    assert claim is not None
    return claim


def _state(factory: sessionmaker[Session], job_id: str = "job-1") -> _PersistedState:
    with factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        lease = session.get(JobLease, job_id)
        dead_letter = session.get(JobDeadLetter, job_id)
        attempts = session.scalars(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.attempt_number)
        ).all()
        return {
            "job": {
                "status": job.status,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "fence_token": job.fence_token,
                "run_after": job.run_after,
                "updated_at": job.updated_at,
                "last_error_class": job.last_error_class,
            },
            "lease": None
            if lease is None
            else {
                "owner": lease.lease_owner,
                "leased_until": lease.leased_until,
                "fence_token": lease.fence_token,
            },
            "attempts": [
                {
                    "id": attempt.id,
                    "attempt_number": attempt.attempt_number,
                    "fence_token": attempt.fence_token,
                    "owner": attempt.lease_owner,
                    "status": attempt.status,
                    "error_class": attempt.error_class,
                    "message": attempt.redacted_error_message,
                    "started_at": attempt.started_at,
                    "finished_at": attempt.finished_at,
                }
                for attempt in attempts
            ],
            "dead_letter": None
            if dead_letter is None
            else {
                "tenant_id": dead_letter.tenant_id,
                "attempt_number": dead_letter.attempt_number,
                "fence_token": dead_letter.fence_token,
                "error_class": dead_letter.error_class,
                "message": dead_letter.redacted_error_message,
                "dead_lettered_at": dead_letter.dead_lettered_at,
            },
        }


def _repository_pair(
    database_url: str,
) -> tuple[JobRepository, JobRepository, Engine, Engine]:
    settings = Settings(database_url=database_url)
    engine_a = create_db_engine(settings)
    engine_b = create_db_engine(settings)
    return (
        JobRepository(create_session_factory(engine_a)),
        JobRepository(create_session_factory(engine_b)),
        engine_a,
        engine_b,
    )


def _race(
    left: Callable[[], object],
    right: Callable[[], object],
) -> tuple[object, object]:
    barrier = threading.Barrier(3)
    results: list[object] = [None, None]
    errors: list[BaseException] = []

    def run(index: int, operation: Callable[[], object]) -> None:
        try:
            barrier.wait(timeout=5)
            results[index] = operation()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(0, left)),
        threading.Thread(target=run, args=(1, right)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not errors
    return results[0], results[1]


def test_claim_persists_one_running_attempt(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_job(session_factory)
    claim = _claim(JobRepository(session_factory))

    assert claim.attempts == 1
    assert claim.fence_token == 1
    state = _state(session_factory)
    assert state["job"] == {
        "status": JobStatus.LEASED.value,
        "attempts": 1,
        "max_attempts": 5,
        "fence_token": 1,
        "run_after": to_utc_rfc3339(T0),
        "updated_at": to_utc_rfc3339(T0),
        "last_error_class": None,
    }
    assert state["lease"] == {
        "owner": "worker-a",
        "leased_until": to_utc_rfc3339(T0 + LEASE_TTL),
        "fence_token": 1,
    }
    assert state["attempts"] == [
        {
            "id": "job-1:attempt:1",
            "attempt_number": 1,
            "fence_token": 1,
            "owner": "worker-a",
            "status": JobAttemptStatus.RUNNING.value,
            "error_class": None,
            "message": None,
            "started_at": to_utc_rfc3339(T0),
            "finished_at": None,
        }
    ]
    assert state["dead_letter"] is None


def test_complete_marks_attempt_succeeded_and_removes_lease(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_job(session_factory)
    repository = JobRepository(session_factory)
    claim = _claim(repository)

    assert repository.complete_job(
        job_id=claim.job_id,
        owner=claim.owner,
        fence_token=claim.fence_token,
        now=FAIL_AT,
    )

    state = _state(session_factory)
    assert state["job"]["status"] == JobStatus.SUCCEEDED.value
    assert state["job"]["attempts"] == 1
    assert state["job"]["fence_token"] == 1
    assert state["job"]["updated_at"] == to_utc_rfc3339(FAIL_AT)
    assert state["attempts"][0]["status"] == JobAttemptStatus.SUCCEEDED.value
    assert state["attempts"][0]["finished_at"] == to_utc_rfc3339(FAIL_AT)
    assert state["lease"] is None
    assert state["dead_letter"] is None


def test_retryable_failure_schedules_then_claims_attempt_two(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_job(session_factory)
    repository = JobRepository(session_factory)
    claim = _claim(repository)
    retry_delay = timedelta(seconds=17)
    run_after = FAIL_AT + retry_delay

    result = repository.fail_job(
        job_id=claim.job_id,
        owner=claim.owner,
        fence_token=claim.fence_token,
        retryable=True,
        retry_delay=retry_delay,
        error_class="TransientError",
        redacted_error_message="safe summary",
        now=FAIL_AT,
    )

    assert result is not None
    assert result.disposition is JobFailureDisposition.RETRY_SCHEDULED
    assert result.attempt_number == 1
    assert result.fence_token == 2
    assert result.run_after == to_utc_rfc3339(run_after)
    failed_state = _state(session_factory)
    assert failed_state["job"]["status"] == JobStatus.READY.value
    assert failed_state["job"]["attempts"] == 1
    assert failed_state["job"]["fence_token"] == 2
    assert failed_state["job"]["run_after"] == to_utc_rfc3339(run_after)
    assert failed_state["job"]["last_error_class"] == "TransientError"
    assert failed_state["attempts"][0]["status"] == JobAttemptStatus.RETRY_SCHEDULED.value
    assert failed_state["attempts"][0]["error_class"] == "TransientError"
    assert failed_state["attempts"][0]["message"] == "safe summary"
    assert failed_state["attempts"][0]["finished_at"] == to_utc_rfc3339(FAIL_AT)
    assert failed_state["lease"] is None

    second_claim = _claim(repository, owner="worker-b", now=run_after)
    assert second_claim.attempts == 2
    assert second_claim.fence_token == 3
    claimed_state = _state(session_factory)
    assert claimed_state["job"]["status"] == JobStatus.LEASED.value
    assert claimed_state["job"]["attempts"] == 2
    assert claimed_state["job"]["fence_token"] == 3
    assert [attempt["status"] for attempt in claimed_state["attempts"]] == [
        JobAttemptStatus.RETRY_SCHEDULED.value,
        JobAttemptStatus.RUNNING.value,
    ]
    assert claimed_state["attempts"][1]["attempt_number"] == 2
    assert claimed_state["attempts"][1]["fence_token"] == 3
    assert claimed_state["attempts"][1]["owner"] == "worker-b"


@pytest.mark.parametrize(
    ("retryable", "max_attempts"),
    [(False, 5), (True, 1)],
    ids=["nonretryable", "max-attempt-retryable"],
)
def test_failure_dead_letters(
    session_factory: sessionmaker[Session],
    *,
    retryable: bool,
    max_attempts: int,
) -> None:
    _seed_job(session_factory, max_attempts=max_attempts)
    repository = JobRepository(session_factory)
    claim = _claim(repository)

    result = repository.fail_job(
        job_id=claim.job_id,
        owner=claim.owner,
        fence_token=claim.fence_token,
        retryable=retryable,
        retry_delay=timedelta(seconds=10),
        error_class="PermanentError",
        redacted_error_message="safe failure",
        now=FAIL_AT,
    )

    assert result is not None
    assert result.disposition is JobFailureDisposition.DEAD_LETTERED
    assert result.run_after is None
    state = _state(session_factory)
    assert state["job"]["status"] == JobStatus.DEAD_LETTER.value
    assert state["job"]["fence_token"] == 2
    assert state["job"]["last_error_class"] == "PermanentError"
    assert state["attempts"][0]["status"] == JobAttemptStatus.DEAD_LETTER.value
    assert state["attempts"][0]["error_class"] == "PermanentError"
    assert state["attempts"][0]["message"] == "safe failure"
    assert state["lease"] is None
    assert state["dead_letter"] == {
        "tenant_id": "tenant-1",
        "attempt_number": 1,
        "fence_token": 2,
        "error_class": "PermanentError",
        "message": "safe failure",
        "dead_lettered_at": to_utc_rfc3339(FAIL_AT),
    }


@pytest.mark.parametrize(
    "invalid_claim",
    ["wrong-owner", "stale-fence", "expired-lease", "missing-attempt"],
)
def test_fail_rejects_invalid_claim_without_mutation(
    session_factory: sessionmaker[Session],
    invalid_claim: str,
) -> None:
    _seed_job(session_factory)
    repository = JobRepository(session_factory)
    claim = _claim(repository)
    owner = claim.owner
    fence_token = claim.fence_token
    now = FAIL_AT

    if invalid_claim == "wrong-owner":
        owner = "worker-b"
    elif invalid_claim == "stale-fence":
        fence_token -= 1
    elif invalid_claim == "expired-lease":
        now = T0 + LEASE_TTL
    else:
        with session_factory() as session, session.begin():
            attempt = session.get(JobAttempt, "job-1:attempt:1")
            assert attempt is not None
            session.delete(attempt)

    before = _state(session_factory)
    result = repository.fail_job(
        job_id=claim.job_id,
        owner=owner,
        fence_token=fence_token,
        retryable=True,
        retry_delay=timedelta(seconds=10),
        error_class="TransientError",
        redacted_error_message="safe summary",
        now=now,
    )

    assert result is None
    assert _state(session_factory) == before


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"retry_delay": timedelta(seconds=-1)}, "non-negative"),
        ({"error_class": ""}, "non-empty"),
        ({"now": datetime(2026, 7, 13, 12, 0, 1)}, "timezone-aware"),
        ({"redacted_error_message": "x" * 501}, "at most 500"),
    ],
    ids=["invalid-delay", "empty-error-class", "naive-now", "message-too-long"],
)
def test_fail_validates_inputs(
    session_factory: sessionmaker[Session],
    overrides: dict[str, object],
    match: str,
) -> None:
    _seed_job(session_factory)
    repository = JobRepository(session_factory)
    claim = _claim(repository)
    arguments: dict[str, object] = {
        "job_id": claim.job_id,
        "owner": claim.owner,
        "fence_token": claim.fence_token,
        "retryable": True,
        "retry_delay": timedelta(seconds=10),
        "error_class": "TransientError",
        "redacted_error_message": "safe summary",
        "now": FAIL_AT,
    }
    arguments.update(overrides)
    before = _state(session_factory)

    with pytest.raises(ValueError, match=match):
        repository.fail_job(**arguments)  # type: ignore[arg-type]

    assert _state(session_factory) == before


def test_requeue_expired_lease_records_history(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_job(session_factory, max_attempts=2)
    repository = JobRepository(session_factory)
    _claim(repository)
    expired_at = T0 + LEASE_TTL

    assert repository.requeue_expired_leases(now=expired_at) == 1

    state = _state(session_factory)
    assert state["job"]["status"] == JobStatus.READY.value
    assert state["job"]["attempts"] == 1
    assert state["job"]["fence_token"] == 2
    assert state["job"]["last_error_class"] == WORKER_LEASE_EXPIRED
    assert state["attempts"][0]["status"] == JobAttemptStatus.LEASE_EXPIRED.value
    assert state["attempts"][0]["error_class"] == WORKER_LEASE_EXPIRED
    assert state["attempts"][0]["finished_at"] == to_utc_rfc3339(expired_at)
    assert state["lease"] is None
    assert state["dead_letter"] is None


def test_expired_max_attempt_writes_dead_letter_history(
    session_factory: sessionmaker[Session],
) -> None:
    _seed_job(session_factory, max_attempts=1)
    repository = JobRepository(session_factory)
    _claim(repository)
    expired_at = T0 + LEASE_TTL

    assert repository.dead_letter_expired_jobs(now=expired_at) == 1

    state = _state(session_factory)
    assert state["job"]["status"] == JobStatus.DEAD_LETTER.value
    assert state["job"]["fence_token"] == 2
    assert state["job"]["last_error_class"] == WORKER_LEASE_EXPIRED
    assert state["attempts"][0]["status"] == JobAttemptStatus.DEAD_LETTER.value
    assert state["attempts"][0]["error_class"] == WORKER_LEASE_EXPIRED
    assert state["attempts"][0]["finished_at"] == to_utc_rfc3339(expired_at)
    assert state["lease"] is None
    assert state["dead_letter"] == {
        "tenant_id": "tenant-1",
        "attempt_number": 1,
        "fence_token": 2,
        "error_class": WORKER_LEASE_EXPIRED,
        "message": None,
        "dead_lettered_at": to_utc_rfc3339(expired_at),
    }


def test_concurrent_complete_vs_fail_has_one_consistent_winner(
    session_factory: sessionmaker[Session],
    temp_db_url: str,
) -> None:
    _seed_job(session_factory)
    complete_repo, fail_repo, engine_a, engine_b = _repository_pair(temp_db_url)
    try:
        claim = _claim(complete_repo)
        complete_result, fail_result = _race(
            lambda: complete_repo.complete_job(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token,
                now=FAIL_AT,
            ),
            lambda: fail_repo.fail_job(
                job_id=claim.job_id,
                owner=claim.owner,
                fence_token=claim.fence_token,
                retryable=True,
                retry_delay=timedelta(seconds=10),
                error_class="TransientError",
                redacted_error_message="safe summary",
                now=FAIL_AT,
            ),
        )
    finally:
        engine_a.dispose()
        engine_b.dispose()

    assert (complete_result is True) + (fail_result is not None) == 1
    state = _state(session_factory)
    assert state["lease"] is None
    assert len(state["attempts"]) == 1
    if complete_result is True:
        assert fail_result is None
        assert state["job"]["status"] == JobStatus.SUCCEEDED.value
        assert state["job"]["fence_token"] == 1
        assert state["attempts"][0]["status"] == JobAttemptStatus.SUCCEEDED.value
        assert state["dead_letter"] is None
    else:
        assert fail_result is not None
        assert state["job"]["status"] == JobStatus.READY.value
        assert state["job"]["fence_token"] == 2
        assert state["attempts"][0]["status"] == JobAttemptStatus.RETRY_SCHEDULED.value
        assert state["attempts"][0]["error_class"] == "TransientError"
        assert state["dead_letter"] is None


def test_concurrent_expired_reapers_cannot_both_win(
    session_factory: sessionmaker[Session],
    temp_db_url: str,
) -> None:
    _seed_job(session_factory, max_attempts=1)
    requeue_repo, dead_letter_repo, engine_a, engine_b = _repository_pair(temp_db_url)
    try:
        _claim(requeue_repo)
        expired_at = T0 + LEASE_TTL
        requeue_count, dead_letter_count = _race(
            lambda: requeue_repo.requeue_expired_leases(now=expired_at),
            lambda: dead_letter_repo.dead_letter_expired_jobs(now=expired_at),
        )
    finally:
        engine_a.dispose()
        engine_b.dispose()

    assert requeue_count in {0, 1}
    assert dead_letter_count in {0, 1}
    assert requeue_count + dead_letter_count == 1
    assert (requeue_count, dead_letter_count) != (1, 1)
    state = _state(session_factory)
    assert state["job"]["status"] == JobStatus.DEAD_LETTER.value
    assert state["attempts"][0]["status"] == JobAttemptStatus.DEAD_LETTER.value
    assert state["lease"] is None
    assert state["dead_letter"] is not None
