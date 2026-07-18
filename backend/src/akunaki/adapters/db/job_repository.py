"""SQLAlchemy 2 job repository: CAS claim, leases, and leader fencing.

Atomic claim uses candidate discovery plus conditional UPDATE compare-and-swap.
Never uses Postgres-style row locks or lock-skipping claim idioms.
Short transactions only. Unexpected database errors are not swallowed.

Local libsql-experimental may report PRAGMA busy_timeout as set but still raise
``database is locked`` under concurrent writers. Write paths apply a bounded
retry only for that lock contention class so short CAS races wait rather than
failing immediately; all other errors propagate unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy import and_, case, delete, exists, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Job, JobAttempt, JobDeadLetter, JobLease, LeaderLease
from akunaki.domain.jobs import (
    EnqueuedJob,
    JobAttemptStatus,
    JobCandidate,
    JobClaim,
    JobFailureDisposition,
    JobFailureResult,
    JobRole,
    JobStatus,
    LeaderClaim,
    require_aware,
    to_utc_rfc3339,
)

T = TypeVar("T")

# Canonical lease timestamps serialize at second resolution (to_utc_rfc3339).
# Subsecond positive TTLs would collapse to immediate expiry after serialization.
MIN_LEASE_TTL = timedelta(seconds=1)

# Normal short-transaction retry budget for repository writes.  Compatible with
# engine BUSY_TIMEOUT_MS=50 (driver wait).  Each retry gets a fresh Session;
# pooled checkouts (QueuePool) provide real DB-API connection reuse without a
# connection storm.
_BUSY_RETRY_BUDGET_S = 2.0

# Outer claim_next polling budget (monotonic deadline for the full
# discover-then-CAS-loop cycle).  Returns None when exhausted.
_CLAIM_NEXT_BUDGET_S = 0.25

_MAX_REDACTED_ERROR_MESSAGE_LENGTH = 500
_WORKER_LEASE_EXPIRED = "worker_lease_expired"


def _is_database_locked(exc: BaseException) -> bool:
    """Return True only for SQLite/libSQL lock-contention errors."""
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _affected_rows(result: object) -> int:
    """Return integer rowcount from a SQLAlchemy DML result."""
    rowcount = getattr(result, "rowcount", None)
    if not isinstance(rowcount, int):
        msg = "statement result missing integer rowcount"
        raise RuntimeError(msg)
    return rowcount


def _require_nonempty(value: str, *, field_name: str) -> str:
    if not value:
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)
    return value


def _require_lease_ttl(lease_ttl: timedelta) -> None:
    """Reject non-positive and sub-second TTLs (second-resolution serialization)."""
    if lease_ttl < MIN_LEASE_TTL:
        msg = (
            "lease_ttl must be at least one second "
            "(canonical timestamps use second resolution; "
            "a positive subsecond TTL would serialize to immediate expiry)"
        )
        raise ValueError(msg)


def _job_attempt_id(job_id: str, attempt_number: int) -> str:
    """Build the stable primary key for one numbered job attempt."""
    return f"{job_id}:attempt:{attempt_number}"


def _require_failure_details(
    *,
    retry_delay: timedelta,
    error_class: str,
    redacted_error_message: str | None,
) -> None:
    _require_nonempty(error_class, field_name="error_class")
    if retry_delay < timedelta(0):
        msg = "retry_delay must be non-negative"
        raise ValueError(msg)
    if (
        redacted_error_message is not None
        and len(redacted_error_message) > _MAX_REDACTED_ERROR_MESSAGE_LENGTH
    ):
        msg = "redacted_error_message must be at most 500 characters"
        raise ValueError(msg)


class JobRepository:
    """Local libSQL/SQLite durable job lease and leader fencing adapter."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Short-tx runner
    # ------------------------------------------------------------------

    def _run_short_tx(
        self,
        work: Callable[[Session], T],
        *,
        retry_budget_s: float = _BUSY_RETRY_BUDGET_S,
    ) -> T:
        """Run ``work`` in a short transaction with bounded lock-contention retry.

        Each retry opens a fresh Session (and checks out a pooled DB-API
        connection from QueuePool).  On ``database is locked`` /
        ``database is busy`` the transaction is rolled back, the session is
        closed, and a new session retries within the budget; all other errors
        propagate unchanged.  Pooled checkouts provide real DB-API connection
        reuse without the connection storm that NullPool would cause.
        """
        if retry_budget_s <= 0:
            msg = "retry_budget_s must be > 0"
            raise ValueError(msg)
        deadline = time.monotonic() + retry_budget_s
        while True:
            session: Session = self._session_factory()
            try:
                with session.begin():
                    return work(session)
            except Exception as exc:
                if not _is_database_locked(exc) or time.monotonic() >= deadline:
                    raise
            finally:
                session.close()

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

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
        """Insert one ready job, deduplicating on ``(tenant_id, idempotency_key)``.

        Dedupe is a single ``INSERT ... ON CONFLICT DO NOTHING`` followed by a
        read of the surviving row, so two concurrent enqueues of the same key
        cannot both insert and neither raises. A ``None`` key always inserts
        (SQL ``NULL`` never conflicts under the unique constraint).
        """
        _require_nonempty(job_id, field_name="job_id")
        _require_nonempty(tenant_id, field_name="tenant_id")
        _require_nonempty(job_type, field_name="job_type")
        _require_nonempty(payload_json, field_name="payload_json")
        if max_attempts < 1:
            msg = "max_attempts must be >= 1"
            raise ValueError(msg)
        if idempotency_key is not None:
            _require_nonempty(idempotency_key, field_name="idempotency_key")

        now_aware = require_aware(now, field_name="now")
        now_s = to_utc_rfc3339(now_aware)
        run_after_s = to_utc_rfc3339(
            require_aware(run_after, field_name="run_after") if run_after is not None else now_aware
        )

        def work(session: Session) -> EnqueuedJob:
            if idempotency_key is not None:
                existing = session.execute(
                    select(Job.id, Job.job_type, Job.role).where(
                        Job.tenant_id == tenant_id,
                        Job.idempotency_key == idempotency_key,
                    )
                ).one_or_none()
                if existing is not None:
                    existing_id, existing_type, existing_role = existing
                    return EnqueuedJob(
                        job_id=existing_id,
                        tenant_id=tenant_id,
                        job_type=existing_type,
                        role=JobRole(existing_role),
                        created=False,
                    )

            result = session.execute(
                sqlite_insert(Job)
                .values(
                    id=job_id,
                    tenant_id=tenant_id,
                    role=role.value,
                    status=JobStatus.READY.value,
                    payload_json=payload_json,
                    priority=priority,
                    run_after=run_after_s,
                    attempts=0,
                    max_attempts=max_attempts,
                    idempotency_key=idempotency_key,
                    fence_token=0,
                    created_at=now_s,
                    updated_at=now_s,
                    job_type=job_type,
                )
                .on_conflict_do_nothing()
            )
            if _affected_rows(result) == 1:
                return EnqueuedJob(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    job_type=job_type,
                    role=role,
                    created=True,
                )

            # Lost an insert race (or the job id itself already exists). Read
            # the surviving row so the caller always gets a usable identity.
            if idempotency_key is not None:
                winner = session.execute(
                    select(Job.id, Job.job_type, Job.role).where(
                        Job.tenant_id == tenant_id,
                        Job.idempotency_key == idempotency_key,
                    )
                ).one_or_none()
                if winner is not None:
                    winner_id, winner_type, winner_role = winner
                    return EnqueuedJob(
                        job_id=winner_id,
                        tenant_id=tenant_id,
                        job_type=winner_type,
                        role=JobRole(winner_role),
                        created=False,
                    )

            # No idempotency key: the conflict can only be the primary key, so
            # this job id is already taken by a different logical job.
            msg = f"job id {job_id!r} already exists"
            raise ValueError(msg)

        return self._run_short_tx(work)

    # ------------------------------------------------------------------
    # Private in-session helpers (discovery + claim construction)
    # ------------------------------------------------------------------

    def _discover_due_rows(
        self,
        session: Session,
        *,
        role: JobRole,
        now_s: str,
        limit: int,
    ) -> Sequence[Job]:
        """Non-locking in-session discovery of due candidates (raw ORM rows)."""
        stmt = (
            select(Job)
            .where(
                Job.status == JobStatus.READY.value,
                Job.role == role.value,
                Job.run_after <= now_s,
                Job.attempts < Job.max_attempts,
            )
            .order_by(Job.priority.asc(), Job.created_at.asc(), Job.id.asc())
            .limit(limit)
        )
        return session.scalars(stmt).all()

    @staticmethod
    def _build_candidate(row: Job) -> JobCandidate:
        """Map an ORM Job row to a domain JobCandidate."""
        return JobCandidate(
            job_id=row.id,
            tenant_id=row.tenant_id,
            role=JobRole(row.role),
            expected_fence_token=row.fence_token,
            priority=row.priority,
            run_after=row.run_after,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            created_at=row.created_at,
        )

    @staticmethod
    def _build_claim(
        session: Session,
        *,
        row: Job,
        owner: str,
        now_s: str,
        leased_until: str,
    ) -> JobClaim:
        """Read back the claimed job and construct a domain JobClaim.

        Must be called inside the same session that performed the CAS UPDATE
        so the in-memory identity map reflects the new fence_token.
        """
        job = session.get(Job, row.id)
        if job is None:  # pragma: no cover - CAS won implies row exists
            msg = f"job {row.id} missing after successful claim"
            raise RuntimeError(msg)

        new_fence = job.fence_token
        session.execute(delete(JobLease).where(JobLease.job_id == row.id))
        session.add(
            JobLease(
                job_id=row.id,
                lease_owner=owner,
                leased_until=leased_until,
                fence_token=new_fence,
                created_at=now_s,
                updated_at=now_s,
            )
        )
        session.add(
            JobAttempt(
                id=_job_attempt_id(job.id, job.attempts),
                job_id=job.id,
                attempt_number=job.attempts,
                fence_token=new_fence,
                lease_owner=owner,
                status=JobAttemptStatus.RUNNING.value,
                error_class=None,
                redacted_error_message=None,
                started_at=now_s,
                finished_at=None,
            )
        )
        session.flush()
        return JobClaim(
            job_id=job.id,
            tenant_id=job.tenant_id,
            role=JobRole(job.role),
            job_type=job.job_type,
            owner=owner,
            fence_token=new_fence,
            leased_until=leased_until,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            payload_json=job.payload_json,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover_due_candidates(
        self,
        *,
        role: JobRole,
        now: datetime,
        limit: int,
    ) -> Sequence[JobCandidate]:
        if limit < 1:
            msg = "limit must be >= 1"
            raise ValueError(msg)
        now_s = to_utc_rfc3339(now)

        def work(session: Session) -> Sequence[JobCandidate]:
            rows = self._discover_due_rows(session, role=role, now_s=now_s, limit=limit)
            return tuple(self._build_candidate(row) for row in rows)

        # Read path still uses the short-tx helper so concurrent WAL readers
        # wait briefly under write lock instead of failing immediately.
        return self._run_short_tx(work)

    def try_claim_job(
        self,
        candidate: JobCandidate,
        *,
        owner: str,
        lease_ttl: timedelta,
        now: datetime,
    ) -> JobClaim | None:
        _require_nonempty(owner, field_name="owner")
        _require_lease_ttl(lease_ttl)
        now_s = to_utc_rfc3339(now)
        leased_until = to_utc_rfc3339(require_aware(now) + lease_ttl)

        def work(session: Session) -> JobClaim | None:
            # Conditional CAS: ready + due + expected fence + role + remaining attempts.
            result = session.execute(
                update(Job)
                .where(
                    Job.id == candidate.job_id,
                    Job.status == JobStatus.READY.value,
                    Job.run_after <= now_s,
                    Job.fence_token == candidate.expected_fence_token,
                    Job.role == candidate.role.value,
                    Job.attempts < Job.max_attempts,
                )
                .values(
                    status=JobStatus.LEASED.value,
                    attempts=Job.attempts + 1,
                    fence_token=Job.fence_token + 1,
                    updated_at=now_s,
                )
            )
            if _affected_rows(result) != 1:
                return None

            # Fetch the raw ORM row for _build_claim (needs current fence_token).
            row = session.get(Job, candidate.job_id)
            if row is None:  # pragma: no cover - CAS won implies row exists
                msg = f"job {candidate.job_id} missing after successful claim"
                raise RuntimeError(msg)

            return self._build_claim(
                session,
                row=row,
                owner=owner,
                now_s=now_s,
                leased_until=leased_until,
            )

        return self._run_short_tx(work)

    def claim_next(
        self,
        *,
        role: JobRole,
        owner: str,
        lease_ttl: timedelta,
        now: datetime,
        limit: int = 32,
    ) -> JobClaim | None:
        """Discover due candidates then claim the first CAS winner.

        Discovery runs in a separate non-locking read transaction.  Each
        candidate CAS attempt runs in its own short write transaction.
        One overall 0.25 s monotonic deadline governs the full cycle; only
        remaining time is passed to each short transaction.  If database
        locked or busy exhausts the claim deadline, returns ``None`` as the
        documented polling outcome meaning no claim was obtained.  Non-lock
        errors propagate.
        """
        _require_nonempty(owner, field_name="owner")
        _require_lease_ttl(lease_ttl)
        if limit < 1:
            msg = "limit must be >= 1"
            raise ValueError(msg)

        now_s = to_utc_rfc3339(now)
        leased_until = to_utc_rfc3339(require_aware(now) + lease_ttl)
        claim_deadline = time.monotonic() + _CLAIM_NEXT_BUDGET_S

        def _discover(session: Session) -> Sequence[Job]:
            return self._discover_due_rows(session, role=role, now_s=now_s, limit=limit)

        def _try_claim(session: Session, row: Job) -> JobClaim | None:
            result = session.execute(
                update(Job)
                .where(
                    Job.id == row.id,
                    Job.status == JobStatus.READY.value,
                    Job.run_after <= now_s,
                    Job.fence_token == row.fence_token,
                    Job.role == role.value,
                    Job.attempts < Job.max_attempts,
                )
                .values(
                    status=JobStatus.LEASED.value,
                    attempts=Job.attempts + 1,
                    fence_token=Job.fence_token + 1,
                    updated_at=now_s,
                )
            )
            if _affected_rows(result) != 1:
                return None

            return self._build_claim(
                session,
                row=row,
                owner=owner,
                now_s=now_s,
                leased_until=leased_until,
            )

        while True:
            remaining = max(claim_deadline - time.monotonic(), 0)
            if remaining <= 0:
                return None

            try:
                candidates = self._run_short_tx(_discover, retry_budget_s=remaining)
                if not candidates:
                    return None

                for row in candidates:
                    remaining = max(claim_deadline - time.monotonic(), 0)
                    if remaining <= 0:
                        return None

                    def _claim_one(session: Session, _row: Job = row) -> JobClaim | None:
                        return _try_claim(session, _row)

                    claim = self._run_short_tx(
                        _claim_one,
                        retry_budget_s=remaining,
                    )
                    if claim is not None:
                        return claim

            except Exception as exc:
                if not _is_database_locked(exc):
                    raise
                # Lock contention within a short tx; check outer deadline.
                if time.monotonic() >= claim_deadline:
                    return None
                # Rediscovery loop continues within the outer deadline.

        # Unreachable but satisfies type checker.
        return None  # pragma: no cover

    def heartbeat_job(
        self,
        *,
        job_id: str,
        owner: str,
        fence_token: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        _require_nonempty(job_id, field_name="job_id")
        _require_nonempty(owner, field_name="owner")
        _require_lease_ttl(lease_ttl)
        now_s = to_utc_rfc3339(now)
        new_until = to_utc_rfc3339(require_aware(now) + lease_ttl)

        def work(session: Session) -> bool:
            # Lease row + jobs row must both remain leased with the same fence.
            job_still_leased = exists(
                select(1).where(
                    Job.id == job_id,
                    Job.status == JobStatus.LEASED.value,
                    Job.fence_token == fence_token,
                )
            )
            result = session.execute(
                update(JobLease)
                .where(
                    JobLease.job_id == job_id,
                    JobLease.lease_owner == owner,
                    JobLease.fence_token == fence_token,
                    JobLease.leased_until > now_s,
                    job_still_leased,
                )
                .values(
                    # Never shorten: keep the later of current expiry and now+ttl.
                    leased_until=case(
                        (JobLease.leased_until > new_until, JobLease.leased_until),
                        else_=new_until,
                    ),
                    updated_at=now_s,
                )
            )
            return _affected_rows(result) == 1

        return self._run_short_tx(work)

    def complete_job(
        self,
        *,
        job_id: str,
        owner: str,
        fence_token: int,
        now: datetime,
    ) -> bool:
        _require_nonempty(job_id, field_name="job_id")
        _require_nonempty(owner, field_name="owner")
        now_s = to_utc_rfc3339(now)

        def work(session: Session) -> bool:
            active = session.execute(
                select(Job.attempts)
                .join(
                    JobLease,
                    and_(
                        JobLease.job_id == Job.id,
                        JobLease.lease_owner == owner,
                        JobLease.fence_token == fence_token,
                        JobLease.leased_until > now_s,
                    ),
                )
                .join(
                    JobAttempt,
                    and_(
                        JobAttempt.job_id == Job.id,
                        JobAttempt.attempt_number == Job.attempts,
                        JobAttempt.fence_token == fence_token,
                        JobAttempt.lease_owner == owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    ),
                )
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.LEASED.value,
                    Job.fence_token == fence_token,
                    ~exists(select(1).where(JobDeadLetter.job_id == job_id)),
                )
            ).scalar_one_or_none()
            if active is None:
                return False

            # A savepoint guarantees any unexpected later CAS mismatch rolls
            # back the whole lifecycle transition before returning False.
            with session.begin_nested() as lifecycle_tx:
                matching_lease = exists(
                    select(1).where(
                        JobLease.job_id == job_id,
                        JobLease.lease_owner == owner,
                        JobLease.fence_token == fence_token,
                        JobLease.leased_until > now_s,
                    )
                )
                matching_running_attempt = exists(
                    select(1).where(
                        JobAttempt.job_id == job_id,
                        JobAttempt.attempt_number == active,
                        JobAttempt.fence_token == fence_token,
                        JobAttempt.lease_owner == owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    )
                )
                job_result = session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == JobStatus.LEASED.value,
                        Job.fence_token == fence_token,
                        Job.attempts == active,
                        matching_lease,
                        matching_running_attempt,
                    )
                    .values(
                        status=JobStatus.SUCCEEDED.value,
                        updated_at=now_s,
                    )
                )
                if _affected_rows(job_result) != 1:
                    lifecycle_tx.rollback()
                    return False

                attempt_result = session.execute(
                    update(JobAttempt)
                    .where(
                        JobAttempt.job_id == job_id,
                        JobAttempt.attempt_number == active,
                        JobAttempt.fence_token == fence_token,
                        JobAttempt.lease_owner == owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=JobAttemptStatus.SUCCEEDED.value,
                        finished_at=now_s,
                    )
                )
                if _affected_rows(attempt_result) != 1:
                    lifecycle_tx.rollback()
                    return False

                lease_result = session.execute(
                    delete(JobLease).where(
                        JobLease.job_id == job_id,
                        JobLease.lease_owner == owner,
                        JobLease.fence_token == fence_token,
                        JobLease.leased_until > now_s,
                    )
                )
                if _affected_rows(lease_result) != 1:
                    lifecycle_tx.rollback()
                    return False
                return True

        return self._run_short_tx(work)

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
        """Record a leased attempt failure and retry or dead-letter atomically."""
        _require_nonempty(job_id, field_name="job_id")
        _require_nonempty(owner, field_name="owner")
        _require_failure_details(
            retry_delay=retry_delay,
            error_class=error_class,
            redacted_error_message=redacted_error_message,
        )
        now_aware = require_aware(now, field_name="now")
        now_s = to_utc_rfc3339(now_aware)
        run_after = to_utc_rfc3339(now_aware + retry_delay)

        def work(session: Session) -> JobFailureResult | None:
            active = session.execute(
                select(Job.tenant_id, Job.attempts, Job.max_attempts)
                .join(
                    JobLease,
                    and_(
                        JobLease.job_id == Job.id,
                        JobLease.lease_owner == owner,
                        JobLease.fence_token == fence_token,
                        JobLease.leased_until > now_s,
                    ),
                )
                .join(
                    JobAttempt,
                    and_(
                        JobAttempt.job_id == Job.id,
                        JobAttempt.attempt_number == Job.attempts,
                        JobAttempt.fence_token == fence_token,
                        JobAttempt.lease_owner == owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    ),
                )
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.LEASED.value,
                    Job.fence_token == fence_token,
                    ~exists(select(1).where(JobDeadLetter.job_id == job_id)),
                )
            ).one_or_none()
            if active is None:
                return None

            tenant_id, attempt_number, max_attempts = active
            should_retry = retryable and attempt_number < max_attempts
            attempt_status = (
                JobAttemptStatus.RETRY_SCHEDULED if should_retry else JobAttemptStatus.DEAD_LETTER
            )
            job_status = JobStatus.READY if should_retry else JobStatus.DEAD_LETTER
            new_fence = fence_token + 1

            with session.begin_nested() as lifecycle_tx:
                matching_lease = exists(
                    select(1).where(
                        JobLease.job_id == job_id,
                        JobLease.lease_owner == owner,
                        JobLease.fence_token == fence_token,
                        JobLease.leased_until > now_s,
                    )
                )
                matching_running_attempt = exists(
                    select(1).where(
                        JobAttempt.job_id == job_id,
                        JobAttempt.attempt_number == attempt_number,
                        JobAttempt.fence_token == fence_token,
                        JobAttempt.lease_owner == owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    )
                )
                job_values: dict[str, object] = {
                    "status": job_status.value,
                    "fence_token": Job.fence_token + 1,
                    "last_error_class": error_class,
                    "updated_at": now_s,
                }
                if should_retry:
                    job_values["run_after"] = run_after
                job_result = session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == JobStatus.LEASED.value,
                        Job.fence_token == fence_token,
                        Job.attempts == attempt_number,
                        matching_lease,
                        matching_running_attempt,
                    )
                    .values(**job_values)
                )
                if _affected_rows(job_result) != 1:
                    lifecycle_tx.rollback()
                    return None

                attempt_result = session.execute(
                    update(JobAttempt)
                    .where(
                        JobAttempt.job_id == job_id,
                        JobAttempt.attempt_number == attempt_number,
                        JobAttempt.fence_token == fence_token,
                        JobAttempt.lease_owner == owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=attempt_status.value,
                        error_class=error_class,
                        redacted_error_message=redacted_error_message,
                        finished_at=now_s,
                    )
                )
                if _affected_rows(attempt_result) != 1:
                    lifecycle_tx.rollback()
                    return None

                lease_result = session.execute(
                    delete(JobLease).where(
                        JobLease.job_id == job_id,
                        JobLease.lease_owner == owner,
                        JobLease.fence_token == fence_token,
                        JobLease.leased_until > now_s,
                    )
                )
                if _affected_rows(lease_result) != 1:
                    lifecycle_tx.rollback()
                    return None

                if not should_retry:
                    session.add(
                        JobDeadLetter(
                            job_id=job_id,
                            tenant_id=tenant_id,
                            attempt_number=attempt_number,
                            fence_token=new_fence,
                            error_class=error_class,
                            redacted_error_message=redacted_error_message,
                            dead_lettered_at=now_s,
                        )
                    )
                    session.flush()

                return JobFailureResult(
                    disposition=(
                        JobFailureDisposition.RETRY_SCHEDULED
                        if should_retry
                        else JobFailureDisposition.DEAD_LETTERED
                    ),
                    job_id=job_id,
                    attempt_number=attempt_number,
                    fence_token=new_fence,
                    run_after=run_after if should_retry else None,
                )

        return self._run_short_tx(work)

    def requeue_expired_leases(self, *, now: datetime) -> int:
        """Requeue expired leases with remaining attempts via per-row fenced CAS.

        Each UPDATE rechecks status=leased, expected fence, attempts remaining,
        a matching expired lease, and the corresponding running attempt. A win
        records lease expiry in attempt history before deleting the exact lease.
        Returns committed lifecycle wins, not discovery count.
        """
        now_s = to_utc_rfc3339(now)

        def work(session: Session) -> int:
            stmt = (
                select(
                    Job.id,
                    Job.attempts,
                    Job.fence_token,
                    JobLease.lease_owner,
                )
                .join(
                    JobLease,
                    and_(
                        JobLease.job_id == Job.id,
                        JobLease.fence_token == Job.fence_token,
                    ),
                )
                .join(
                    JobAttempt,
                    and_(
                        JobAttempt.job_id == Job.id,
                        JobAttempt.attempt_number == Job.attempts,
                        JobAttempt.fence_token == Job.fence_token,
                        JobAttempt.lease_owner == JobLease.lease_owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    ),
                )
                .where(
                    Job.status == JobStatus.LEASED.value,
                    JobLease.leased_until <= now_s,
                    Job.attempts < Job.max_attempts,
                )
            )
            candidates = list(session.execute(stmt).all())
            wins = 0
            for job_id, attempt_number, fence, owner in candidates:
                with session.begin_nested() as lifecycle_tx:
                    matching_expired_lease = exists(
                        select(1).where(
                            JobLease.job_id == job_id,
                            JobLease.lease_owner == owner,
                            JobLease.fence_token == fence,
                            JobLease.leased_until <= now_s,
                        )
                    )
                    matching_running_attempt = exists(
                        select(1).where(
                            JobAttempt.job_id == job_id,
                            JobAttempt.attempt_number == attempt_number,
                            JobAttempt.fence_token == fence,
                            JobAttempt.lease_owner == owner,
                            JobAttempt.status == JobAttemptStatus.RUNNING.value,
                        )
                    )
                    result = session.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.status == JobStatus.LEASED.value,
                            Job.fence_token == fence,
                            Job.attempts == attempt_number,
                            Job.attempts < Job.max_attempts,
                            matching_expired_lease,
                            matching_running_attempt,
                        )
                        .values(
                            status=JobStatus.READY.value,
                            fence_token=Job.fence_token + 1,
                            last_error_class=_WORKER_LEASE_EXPIRED,
                            updated_at=now_s,
                        )
                    )
                    if _affected_rows(result) != 1:
                        lifecycle_tx.rollback()
                        continue

                    attempt_result = session.execute(
                        update(JobAttempt)
                        .where(
                            JobAttempt.job_id == job_id,
                            JobAttempt.attempt_number == attempt_number,
                            JobAttempt.fence_token == fence,
                            JobAttempt.lease_owner == owner,
                            JobAttempt.status == JobAttemptStatus.RUNNING.value,
                        )
                        .values(
                            status=JobAttemptStatus.LEASE_EXPIRED.value,
                            error_class=_WORKER_LEASE_EXPIRED,
                            finished_at=now_s,
                        )
                    )
                    if _affected_rows(attempt_result) != 1:
                        lifecycle_tx.rollback()
                        continue

                    lease_result = session.execute(
                        delete(JobLease).where(
                            JobLease.job_id == job_id,
                            JobLease.lease_owner == owner,
                            JobLease.fence_token == fence,
                            JobLease.leased_until <= now_s,
                        )
                    )
                    if _affected_rows(lease_result) != 1:
                        lifecycle_tx.rollback()
                        continue
                    wins += 1
            return wins

        return self._run_short_tx(work)

    def dead_letter_expired_jobs(self, *, now: datetime) -> int:
        """Dead-letter max-attempt expired leases via per-row fenced CAS.

        Same fencing as requeue. A win closes the running attempt, records a
        permanent dead letter, and deletes the exact expired lease atomically.
        """
        now_s = to_utc_rfc3339(now)

        def work(session: Session) -> int:
            stmt = (
                select(
                    Job.id,
                    Job.tenant_id,
                    Job.attempts,
                    Job.fence_token,
                    JobLease.lease_owner,
                )
                .join(
                    JobLease,
                    and_(
                        JobLease.job_id == Job.id,
                        JobLease.fence_token == Job.fence_token,
                    ),
                )
                .join(
                    JobAttempt,
                    and_(
                        JobAttempt.job_id == Job.id,
                        JobAttempt.attempt_number == Job.attempts,
                        JobAttempt.fence_token == Job.fence_token,
                        JobAttempt.lease_owner == JobLease.lease_owner,
                        JobAttempt.status == JobAttemptStatus.RUNNING.value,
                    ),
                )
                .where(
                    Job.status == JobStatus.LEASED.value,
                    JobLease.leased_until <= now_s,
                    Job.attempts >= Job.max_attempts,
                    ~exists(select(1).where(JobDeadLetter.job_id == Job.id)),
                )
            )
            candidates = list(session.execute(stmt).all())
            wins = 0
            for job_id, tenant_id, attempt_number, fence, owner in candidates:
                with session.begin_nested() as lifecycle_tx:
                    matching_expired_lease = exists(
                        select(1).where(
                            JobLease.job_id == job_id,
                            JobLease.lease_owner == owner,
                            JobLease.fence_token == fence,
                            JobLease.leased_until <= now_s,
                        )
                    )
                    matching_running_attempt = exists(
                        select(1).where(
                            JobAttempt.job_id == job_id,
                            JobAttempt.attempt_number == attempt_number,
                            JobAttempt.fence_token == fence,
                            JobAttempt.lease_owner == owner,
                            JobAttempt.status == JobAttemptStatus.RUNNING.value,
                        )
                    )
                    result = session.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.status == JobStatus.LEASED.value,
                            Job.fence_token == fence,
                            Job.attempts == attempt_number,
                            Job.attempts >= Job.max_attempts,
                            matching_expired_lease,
                            matching_running_attempt,
                        )
                        .values(
                            status=JobStatus.DEAD_LETTER.value,
                            fence_token=Job.fence_token + 1,
                            last_error_class=_WORKER_LEASE_EXPIRED,
                            updated_at=now_s,
                        )
                    )
                    if _affected_rows(result) != 1:
                        lifecycle_tx.rollback()
                        continue

                    attempt_result = session.execute(
                        update(JobAttempt)
                        .where(
                            JobAttempt.job_id == job_id,
                            JobAttempt.attempt_number == attempt_number,
                            JobAttempt.fence_token == fence,
                            JobAttempt.lease_owner == owner,
                            JobAttempt.status == JobAttemptStatus.RUNNING.value,
                        )
                        .values(
                            status=JobAttemptStatus.DEAD_LETTER.value,
                            error_class=_WORKER_LEASE_EXPIRED,
                            finished_at=now_s,
                        )
                    )
                    if _affected_rows(attempt_result) != 1:
                        lifecycle_tx.rollback()
                        continue

                    lease_result = session.execute(
                        delete(JobLease).where(
                            JobLease.job_id == job_id,
                            JobLease.lease_owner == owner,
                            JobLease.fence_token == fence,
                            JobLease.leased_until <= now_s,
                        )
                    )
                    if _affected_rows(lease_result) != 1:
                        lifecycle_tx.rollback()
                        continue

                    session.add(
                        JobDeadLetter(
                            job_id=job_id,
                            tenant_id=tenant_id,
                            attempt_number=attempt_number,
                            fence_token=fence + 1,
                            error_class=_WORKER_LEASE_EXPIRED,
                            redacted_error_message=None,
                            dead_lettered_at=now_s,
                        )
                    )
                    session.flush()
                    wins += 1
            return wins

        return self._run_short_tx(work)

    def try_acquire_leader(
        self,
        *,
        lease_name: str,
        owner: str,
        lease_ttl: timedelta,
        now: datetime,
    ) -> LeaderClaim | None:
        _require_nonempty(lease_name, field_name="lease_name")
        _require_nonempty(owner, field_name="owner")
        _require_lease_ttl(lease_ttl)
        now_s = to_utc_rfc3339(now)
        leased_until = to_utc_rfc3339(require_aware(now) + lease_ttl)

        def work(session: Session) -> LeaderClaim | None:
            # Ensure a coordination row exists (insert-or-ignore; fence stays 0).
            session.execute(
                sqlite_insert(LeaderLease)
                .values(
                    lease_name=lease_name,
                    lease_owner=None,
                    leased_until=None,
                    fence_token=0,
                    updated_at=now_s,
                )
                .on_conflict_do_nothing(index_elements=["lease_name"])
            )

            # CAS: free (null owner / null expiry) or expired lease may be taken.
            result = session.execute(
                update(LeaderLease)
                .where(
                    LeaderLease.lease_name == lease_name,
                    or_(
                        LeaderLease.lease_owner.is_(None),
                        LeaderLease.leased_until.is_(None),
                        LeaderLease.leased_until <= now_s,
                    ),
                )
                .values(
                    lease_owner=owner,
                    leased_until=leased_until,
                    fence_token=LeaderLease.fence_token + 1,
                    updated_at=now_s,
                )
            )
            if _affected_rows(result) != 1:
                return None

            row = session.get(LeaderLease, lease_name)
            if row is None or row.lease_owner is None or row.leased_until is None:
                msg = f"leader lease {lease_name} missing after successful acquire"
                raise RuntimeError(msg)
            return LeaderClaim(
                lease_name=row.lease_name,
                owner=row.lease_owner,
                fence_token=row.fence_token,
                leased_until=row.leased_until,
            )

        return self._run_short_tx(work)

    def heartbeat_leader(
        self,
        *,
        lease_name: str,
        owner: str,
        fence_token: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        _require_nonempty(lease_name, field_name="lease_name")
        _require_nonempty(owner, field_name="owner")
        _require_lease_ttl(lease_ttl)
        now_s = to_utc_rfc3339(now)
        new_until = to_utc_rfc3339(require_aware(now) + lease_ttl)

        def work(session: Session) -> bool:
            result = session.execute(
                update(LeaderLease)
                .where(
                    LeaderLease.lease_name == lease_name,
                    LeaderLease.lease_owner == owner,
                    LeaderLease.fence_token == fence_token,
                    LeaderLease.leased_until.is_not(None),
                    LeaderLease.leased_until > now_s,
                )
                .values(
                    leased_until=case(
                        (LeaderLease.leased_until > new_until, LeaderLease.leased_until),
                        else_=new_until,
                    ),
                    updated_at=now_s,
                )
            )
            return _affected_rows(result) == 1

        return self._run_short_tx(work)

    def has_valid_leadership(
        self,
        *,
        lease_name: str,
        owner: str,
        fence_token: int,
        now: datetime,
    ) -> bool:
        _require_nonempty(lease_name, field_name="lease_name")
        _require_nonempty(owner, field_name="owner")
        now_s = to_utc_rfc3339(now)

        def work(session: Session) -> bool:
            row = session.execute(
                select(LeaderLease).where(
                    LeaderLease.lease_name == lease_name,
                    LeaderLease.lease_owner == owner,
                    LeaderLease.fence_token == fence_token,
                    LeaderLease.leased_until.is_not(None),
                    LeaderLease.leased_until > now_s,
                )
            ).scalar_one_or_none()
            return row is not None

        return self._run_short_tx(work)

    def has_valid_job_lease(
        self,
        *,
        job_id: str,
        owner: str,
        fence_token: int,
        now: datetime,
    ) -> bool:
        """Return True when job is leased, owner/fence match, and lease is unexpired.

        Lease validity primitive only. Atomic domain side-effect fencing will be
        integrated with the later application unit of work and is not yet claimed.
        """
        _require_nonempty(job_id, field_name="job_id")
        _require_nonempty(owner, field_name="owner")
        now_s = to_utc_rfc3339(now)

        def work(session: Session) -> bool:
            row = session.execute(
                select(JobLease)
                .join(Job, Job.id == JobLease.job_id)
                .where(
                    JobLease.job_id == job_id,
                    JobLease.lease_owner == owner,
                    JobLease.fence_token == fence_token,
                    JobLease.leased_until > now_s,
                    Job.status == JobStatus.LEASED.value,
                    Job.fence_token == fence_token,
                )
            ).scalar_one_or_none()
            return row is not None

        return self._run_short_tx(work)
