"""Fenced unit of work over local libSQL/SQLite.

Runs a domain side effect in **one** transaction that also carries the lease
check, so a worker whose lease expired mid-execution cannot land a write behind
the worker that legitimately took the job over.

Why the check must be inside the write transaction
--------------------------------------------------
Calling ``has_valid_job_lease`` and then writing is a time-of-check /
time-of-use race: the lease can expire in the gap. Here the fence is verified
against the durable ``jobs`` / ``job_leases`` rows **within** the same
transaction as the writes, both before ``work`` runs and again immediately
before commit. The post-check is the load-bearing one — ``work`` may take
seconds, which is exactly the window in which a lease expires — and because it
shares the transaction with the writes, a failure rolls the side effect back
rather than leaving it durable.

The two checks read the same rows a reaper must ``UPDATE`` to requeue the job.
Under SQLite/libSQL this transaction holds a write lock from its first write
onward, so a concurrent reaper serializes against it: either the reaper commits
first and the post-check sees the fence bumped (this transaction rolls back), or
this transaction commits first and the reaper's requeue lands after a completed,
fenced side effect. There is no interleaving in which both succeed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Job, JobLease
from akunaki.domain.jobs import JobClaim, JobStatus, require_aware, to_utc_rfc3339
from akunaki.ports.unit_of_work import LeaseLostError

T = TypeVar("T")

logger = logging.getLogger("akunaki.unit_of_work")

__all__ = ["FencedUnitOfWork"]


class FencedUnitOfWork:
    """Execute job side effects transactionally, fenced by the job lease."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def run_fenced(
        self,
        claim: JobClaim,
        work: Callable[[Session], T],
        *,
        now: datetime,
    ) -> T:
        """Run ``work(session)`` under ``claim``'s fence, atomically.

        ``work`` receives the transaction's session and must perform its writes
        through it — a collaborator that opens its own session escapes the
        fence and commits independently.
        """
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        session = self._session_factory()
        try:
            with session.begin():
                if not _fence_held(session, claim=claim, now_s=now_s):
                    # Never ran ``work``: nothing to roll back but the read.
                    raise LeaseLostError(
                        job_id=claim.job_id,
                        fence_token=claim.fence_token,
                    )

                result = work(session)

                # Re-verify before commit. ``work`` just spent real time; this
                # is the check that actually closes the window, and it shares
                # the transaction with the writes it is guarding.
                session.flush()
                if not _fence_held(session, claim=claim, now_s=now_s):
                    logger.warning(
                        "lease lost during fenced side effect; rolling back",
                        extra={
                            "job_id": claim.job_id,
                            "fence_token": claim.fence_token,
                        },
                    )
                    raise LeaseLostError(
                        job_id=claim.job_id,
                        fence_token=claim.fence_token,
                    )

                return result
        finally:
            session.close()


def _fence_held(session: Session, *, claim: JobClaim, now_s: str) -> bool:
    """True when the job is still leased to this owner at this exact fence.

    Mirrors ``JobRepository.has_valid_job_lease`` but reads inside the caller's
    transaction rather than opening its own, which is the whole point: the
    check and the guarded writes commit or roll back together.
    """
    row = session.execute(
        select(JobLease.job_id)
        .join(Job, Job.id == JobLease.job_id)
        .where(
            JobLease.job_id == claim.job_id,
            JobLease.lease_owner == claim.owner,
            JobLease.fence_token == claim.fence_token,
            JobLease.leased_until > now_s,
            Job.status == JobStatus.LEASED.value,
            Job.fence_token == claim.fence_token,
        )
    ).scalar_one_or_none()
    return row is not None
