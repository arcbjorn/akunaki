"""Fenced unit of work: bind a domain side effect to the job lease that authorized it.

``JobRepositoryPort.has_valid_job_lease`` answers "is this lease valid *now*?".
That is a validity primitive, not fencing: between the check and the write the
lease can expire, the reaper can requeue the job, and another worker can claim
it at a higher fence. The stale worker's write then lands anyway — its
``complete_job`` is correctly rejected, but the side effect is already durable.

For a versioned artifact that is a real corruption, not a harmless duplicate.
Two workers recomputing the same day at different times see different facts, so
their results differ and neither is a no-op under dependency-hash idempotency:
the winner writes version *n*, then the stale worker supersedes it and appends
*n+1*, making the **older** computation the current score with a supersession
chain that records it as legitimate.

This port closes that window by making the fence check part of the *same*
transaction as the side effect. The lease is re-verified against the durable job
row inside the transaction that carries the writes, so a stale worker's work
rolls back atomically rather than landing behind the rightful owner.

Adapters implement this protocol. Domain and ports must not import SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypeVar

from akunaki.domain.jobs import JobClaim

T = TypeVar("T")

__all__ = ["FencedUnitOfWorkPort", "LeaseLostError"]


class LeaseLostError(RuntimeError):
    """Raised when the lease authorizing a side effect is no longer held.

    The transaction carrying the side effect has been rolled back; nothing the
    unit of work wrote is durable. This is a **transient** condition — the job
    is (or will be) owned by another worker, so the current worker must not
    report success, and must not retry the write under the same dead fence.
    """

    def __init__(self, *, job_id: str, fence_token: int) -> None:
        super().__init__(
            f"lease lost for job {job_id!r} at fence {fence_token}; side effect rolled back"
        )
        self.job_id = job_id
        self.fence_token = fence_token


class FencedUnitOfWorkPort(Protocol):
    """Run a domain side effect only while its authorizing job lease is held."""

    def run_fenced(
        self,
        claim: JobClaim,
        work: Callable[..., T],
        *,
        now: datetime,
    ) -> T:
        """Execute ``work`` transactionally, fenced by ``claim``'s lease.

        The lease is verified **inside** the write transaction — before and
        after ``work`` — so the check cannot be overtaken by an expiry between
        validation and commit. On success everything ``work`` wrote commits
        atomically with the fence still held.

        Raises :class:`LeaseLostError` (after rolling back) when the lease is
        not held at either boundary. Exceptions from ``work`` itself propagate
        unchanged, with the transaction rolled back.
        """
        ...
