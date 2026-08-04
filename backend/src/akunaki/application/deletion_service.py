"""Drive the privacy deletion pipeline for a tenant that asked to be erased.

The repository exposes each stage separately because ordering is a safety
property (jobs cancelled before rows are scrubbed, each in its own committed
transaction). This service runs those stages in the one correct order, so no
caller has to remember it, and marks the request ``failed`` — retaining the
stage it reached — if any stage raises.

Deliberately synchronous. The pipeline is a handful of scoped DELETEs, and
running it inline means the caller gets a truthful answer: by the time the
response is written, the data really is gone. Handing it to the worker would
mean acknowledging an erasure that has not happened yet, and the very first
stage cancels that tenant's jobs — including, potentially, the job doing the
work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from akunaki.domain.audit import ActorType, AuditAction
from akunaki.domain.deletion import DeletionStatus, ScrubCounts

logger = logging.getLogger("akunaki.deletion")

__all__ = ["DeletionOutcome", "DeletionPipelinePort", "DeletionService"]


class AuditSink(Protocol):
    """Port: append one audit event."""

    def record(
        self,
        *,
        event_id: str,
        tenant_id: str | None,
        actor_type: ActorType,
        actor_id: str | None,
        action: AuditAction,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, str],
        now: datetime,
    ) -> str:
        """Append the event and return its chain hash."""
        ...


class DeletionPipelinePort(Protocol):
    """The staged deletion pipeline, in its required order."""

    def request(self, *, request_id: str, tenant_id: str, now: datetime) -> str:
        """Record a deletion request in the ``requested`` state."""
        ...

    def cancel_jobs(self, *, request_id: str, now: datetime) -> int:
        """Cancel the tenant's pending and leased jobs; return how many."""
        ...

    def scrub_rows(self, *, request_id: str, now: datetime, jobs_cancelled: int) -> ScrubCounts:
        """Hard-delete the tenant's data; return per-class counts."""
        ...

    def schedule_backup_expiry(self, *, request_id: str, now: datetime) -> None:
        """Record that backup expiry was scheduled."""
        ...

    def complete(
        self,
        *,
        request_id: str,
        proof_id: str,
        counts: ScrubCounts,
        now: datetime,
    ) -> None:
        """Finish the pipeline and write the minimal completion proof."""
        ...

    def fail(self, *, request_id: str, failure_class: str, now: datetime) -> None:
        """Mark a deletion failed, retaining the stage it reached."""
        ...

    def status_of(self, *, request_id: str) -> DeletionStatus | None:
        """Return the current pipeline status, or None when unknown."""
        ...


class DeletionOutcome:
    """What one completed deletion erased.

    Counts only — no identity, no health values — matching the completion
    proof, which is the artifact that outlives the tenant.
    """

    __slots__ = ("counts", "request_id", "status")

    def __init__(self, *, request_id: str, status: DeletionStatus, counts: ScrubCounts) -> None:
        self.request_id = request_id
        self.status = status
        self.counts = counts


class DeletionService:
    """Run the deletion pipeline end to end for one tenant."""

    def __init__(
        self,
        *,
        pipeline: DeletionPipelinePort,
        new_id: Callable[[], str],
        audit: AuditSink | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._new_id = new_id
        self._audit = audit

    def _record_audit(
        self,
        *,
        tenant_id: str,
        request_id: str,
        outcome: str,
        now: datetime,
    ) -> None:
        """Append the audit event, never letting it break the deletion.

        The erasure has already happened by the time this runs; raising here
        would report a failure for work that succeeded, and the tenant's data
        would still be gone. A failed append is logged loudly instead.
        """
        if self._audit is None:
            return
        try:
            self._audit.record(
                event_id=self._new_id(),
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_id=None,
                action=AuditAction.DELETE,
                resource_type="tenant",
                # The deletion request id, not the tenant id: the request is
                # the artifact that outlives the erasure.
                resource_id=request_id,
                metadata={"outcome": outcome},
                now=now,
            )
        except Exception:
            logger.exception(
                "failed to append deletion audit event",
                extra={"deletion_request_id": request_id},
            )

    def delete_tenant(self, *, tenant_id: str, now: datetime) -> DeletionOutcome:
        """Erase a tenant's data, in the pipeline's required order.

        On any stage failure the request is marked ``failed`` — keeping the
        stage it reached, so a partial deletion is visible rather than silently
        reported as done — and the original error propagates.
        """
        request_id = self._new_id()
        self._pipeline.request(request_id=request_id, tenant_id=tenant_id, now=now)

        try:
            cancelled = self._pipeline.cancel_jobs(request_id=request_id, now=now)
            counts = self._pipeline.scrub_rows(
                request_id=request_id,
                now=now,
                jobs_cancelled=cancelled,
            )
            self._pipeline.schedule_backup_expiry(request_id=request_id, now=now)
            self._pipeline.complete(
                request_id=request_id,
                proof_id=self._new_id(),
                counts=counts,
                now=now,
            )
        except Exception as exc:
            # Record the failure against the stage reached, then re-raise: a
            # deletion that half-ran must never read as completed.
            self._pipeline.fail(
                request_id=request_id,
                failure_class=type(exc).__qualname__,
                now=now,
            )
            logger.exception(
                "privacy deletion failed",
                extra={"deletion_request_id": request_id},
            )
            # Audit the attempt even when it failed: "I never asked for a
            # deletion" is exactly the claim the trail has to answer, and a
            # half-run erasure is the case most worth having on record.
            self._record_audit(
                tenant_id=tenant_id,
                request_id=request_id,
                outcome="failed",
                now=now,
            )
            raise

        self._record_audit(
            tenant_id=tenant_id,
            request_id=request_id,
            outcome="completed",
            now=now,
        )

        # No tenant id in the log: the tenant is gone, and the request id is
        # the only handle that legitimately outlives it.
        logger.info(
            "privacy deletion completed",
            extra={
                "deletion_request_id": request_id,
                "rows_scrubbed": counts.total_rows,
            },
        )
        return DeletionOutcome(
            request_id=request_id,
            status=DeletionStatus.COMPLETED,
            counts=counts,
        )
