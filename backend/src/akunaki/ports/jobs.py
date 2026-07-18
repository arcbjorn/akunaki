"""Job concurrency port: durable claim, lease, and leader fencing.

Adapters implement this protocol. Domain and ports must not import SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from akunaki.domain.jobs import (
    EnqueuedJob,
    JobCandidate,
    JobClaim,
    JobFailureResult,
    JobRole,
    LeaderClaim,
)


class JobRepositoryPort(Protocol):
    """Typed operations for job enqueue, CAS claim, lease lifecycle, and leader fencing."""

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

        When a key is supplied and a job already exists for that tenant/key,
        the existing job is returned with ``created=False`` and nothing is
        written. A ``None`` key always inserts. ``run_after`` defaults to
        ``now`` (immediately due).
        """
        ...

    def discover_due_candidates(
        self,
        *,
        role: JobRole,
        now: datetime,
        limit: int,
    ) -> Sequence[JobCandidate]:
        """Return due ready jobs for ``role`` ordered by priority then created_at.

        Non-locking read. Callers use :meth:`try_claim_job` (or
        :meth:`claim_next`) for atomic claim.
        """
        ...

    def try_claim_job(
        self,
        candidate: JobCandidate,
        *,
        owner: str,
        lease_ttl: timedelta,
        now: datetime,
    ) -> JobClaim | None:
        """Attempt CAS claim of one candidate. None means another worker won."""
        ...

    def claim_next(
        self,
        *,
        role: JobRole,
        owner: str,
        lease_ttl: timedelta,
        now: datetime,
        limit: int = 32,
    ) -> JobClaim | None:
        """Discover due candidates and claim the first CAS winner (loser retries).

        Validates owner, lease_ttl, and limit before discovery (including empty queues).
        """
        ...

    def heartbeat_job(
        self,
        *,
        job_id: str,
        owner: str,
        fence_token: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        """Extend lease when job remains leased with matching owner/fence and unexpired lease."""
        ...

    def complete_job(
        self,
        *,
        job_id: str,
        owner: str,
        fence_token: int,
        now: datetime,
    ) -> bool:
        """Mark succeeded only with matching owner and fence on an unexpired lease."""
        ...

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
        ...

    def requeue_expired_leases(self, *, now: datetime) -> int:
        """Requeue leased jobs whose lease expired and still have remaining attempts.

        Per-row fenced CAS: rechecks status=leased, expected fence, attempts
        remaining, and matching expired lease. Increments fence before ready.
        Returns actual CAS wins.
        """
        ...

    def dead_letter_expired_jobs(self, *, now: datetime) -> int:
        """Dead-letter leased jobs with expired leases at max attempts (fenced CAS wins)."""
        ...

    def try_acquire_leader(
        self,
        *,
        lease_name: str,
        owner: str,
        lease_ttl: timedelta,
        now: datetime,
    ) -> LeaderClaim | None:
        """CAS-acquire a named leader lease. None if another owner holds an unexpired lease."""
        ...

    def heartbeat_leader(
        self,
        *,
        lease_name: str,
        owner: str,
        fence_token: int,
        lease_ttl: timedelta,
        now: datetime,
    ) -> bool:
        """Extend leadership when owner, fence, and unexpired lease match."""
        ...

    def has_valid_leadership(
        self,
        *,
        lease_name: str,
        owner: str,
        fence_token: int,
        now: datetime,
    ) -> bool:
        """Return True when owner holds an unexpired leader lease with matching fence."""
        ...

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
        ...
