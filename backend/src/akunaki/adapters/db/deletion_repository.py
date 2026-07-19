"""Privacy deletion: cancel tenant work, then scrub tenant rows.

Ordering is a safety property. Jobs are cancelled **first** and in their own
committed transaction, so no in-flight sync can re-insert rows that the scrub
is about to delete. Only then does the scrub run.

Scope note: this is phase one's **stub**. It hard-deletes the tenant's health
and connection data and writes the minimal completion proof. It does **not**
write a restoration-suppression ledger — that needs a dedicated deletion key
with access separation, which does not exist yet.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import (
    Connection,
    ConnectionHealth,
    ConnectionSecret,
    DeletionCompletionProof,
    DeletionRequest,
    FactRecord,
    Job,
    OAuthState,
    RawObject,
    RawPayload,
    RawRevision,
    SleepSession,
    SyncCursor,
    SyncRun,
    Tenant,
)
from akunaki.domain.deletion import (
    DeletionStatus,
    ScrubCounts,
    require_transition,
)
from akunaki.domain.jobs import JobStatus, require_aware, to_utc_rfc3339

# Job states that still represent pending or running work.
_LIVE_JOB_STATES = (JobStatus.READY.value, JobStatus.LEASED.value)


class DeletionRepository:
    """Drive the privacy deletion pipeline for one tenant."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def request(self, *, request_id: str, tenant_id: str, now: datetime) -> str:
        """Record a deletion request in the ``requested`` state."""
        if not request_id or not tenant_id:
            msg = "request_id and tenant_id must be non-empty"
            raise ValueError(msg)
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            session.add(
                DeletionRequest(
                    id=request_id,
                    tenant_id=tenant_id,
                    status=DeletionStatus.REQUESTED.value,
                    requested_at=now_s,
                )
            )
        return request_id

    def cancel_jobs(self, *, request_id: str, now: datetime) -> int:
        """Cancel the tenant's pending and leased jobs.

        Committed separately from the scrub so that, by the time rows are
        deleted, no job can still be running against them.
        """
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            request = self._locked_request(session, request_id)
            require_transition(DeletionStatus(request.status), DeletionStatus.JOBS_CANCELLED)
            result = session.execute(
                update(Job)
                .where(
                    Job.tenant_id == request.tenant_id,
                    Job.status.in_(_LIVE_JOB_STATES),
                )
                .values(status=JobStatus.CANCELLED.value, updated_at=now_s)
            )
            cancelled = affected_rows(result)
            request.status = DeletionStatus.JOBS_CANCELLED.value
            request.jobs_cancelled_at = now_s
            return cancelled

    def scrub_rows(self, *, request_id: str, now: datetime, jobs_cancelled: int) -> ScrubCounts:
        """Hard-delete the tenant's health and connection data.

        Runs in one transaction: a partial scrub would leave orphaned health
        data behind with no record of which classes were removed.
        """
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            request = self._locked_request(session, request_id)
            require_transition(DeletionStatus(request.status), DeletionStatus.ROWS_SCRUBBED)
            tenant_id = request.tenant_id

            # Count before deleting: cascades make after-the-fact counts wrong.
            counts = ScrubCounts(
                connections=self._count(session, Connection, tenant_id),
                connection_secrets=self._count(session, ConnectionSecret, tenant_id),
                oauth_states=self._count(session, OAuthState, tenant_id),
                raw_payloads=self._count(session, RawPayload, tenant_id),
                raw_revisions=self._count(session, RawRevision, tenant_id),
                raw_objects=self._count(session, RawObject, tenant_id),
                sync_runs=self._count(session, SyncRun, tenant_id),
                sync_cursors=self._count(session, SyncCursor, tenant_id),
                facts=self._count(session, FactRecord, tenant_id),
                jobs_cancelled=jobs_cancelled,
            )

            # Child-first: FK RESTRICT on raw_revisions -> raw_payload means
            # payloads cannot go before the revisions pointing at them.
            models: tuple[Any, ...] = (
                SleepSession,
                FactRecord,
                RawRevision,
                RawObject,
                RawPayload,
                SyncCursor,
                SyncRun,
                OAuthState,
                ConnectionSecret,
                ConnectionHealth,
                Connection,
                Job,
            )
            for model in models:
                session.execute(delete(model).where(model.tenant_id == tenant_id))

            # The tenant row itself goes last.
            session.execute(delete(Tenant).where(Tenant.id == tenant_id))

            request.status = DeletionStatus.ROWS_SCRUBBED.value
            request.rows_scrubbed_at = now_s
            return counts

    def schedule_backup_expiry(self, *, request_id: str, now: datetime) -> None:
        """Mark backup expiry as scheduled.

        A stub: no backup provider is wired, so this records the pipeline stage
        without claiming backups were actually expired.
        """
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            request = self._locked_request(session, request_id)
            require_transition(DeletionStatus(request.status), DeletionStatus.BACKUPS_SCHEDULED)
            request.status = DeletionStatus.BACKUPS_SCHEDULED.value
            request.backups_scheduled_at = now_s

    def complete(
        self,
        *,
        request_id: str,
        proof_id: str,
        counts: ScrubCounts,
        now: datetime,
    ) -> None:
        """Finish the pipeline and write the minimal completion proof."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            request = self._locked_request(session, request_id)
            require_transition(DeletionStatus(request.status), DeletionStatus.COMPLETED)
            request.status = DeletionStatus.COMPLETED.value
            request.completed_at = now_s
            session.add(
                DeletionCompletionProof(
                    id=proof_id,
                    deletion_request_id=request_id,
                    completed_at=now_s,
                    status="completed",
                    # Counts only: no identity, no health values.
                    scrub_counts_json=json.dumps(counts.as_dict(), sort_keys=True),
                )
            )

    def fail(self, *, request_id: str, failure_class: str, now: datetime) -> None:
        """Mark a deletion failed, retaining the stage it reached."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            request = self._locked_request(session, request_id)
            require_transition(DeletionStatus(request.status), DeletionStatus.FAILED)
            request.status = DeletionStatus.FAILED.value
            request.failure_class = failure_class
            request.completed_at = now_s

    def status_of(self, *, request_id: str) -> DeletionStatus | None:
        """Return the current pipeline status, or None when unknown."""
        with self._session_factory() as session:
            row = session.get(DeletionRequest, request_id)
            return DeletionStatus(row.status) if row is not None else None

    @staticmethod
    def _locked_request(session: Session, request_id: str) -> DeletionRequest:
        request = session.get(DeletionRequest, request_id)
        if request is None:
            msg = f"deletion request {request_id!r} not found"
            raise ValueError(msg)
        return request

    @staticmethod
    def _count(session: Session, model: Any, tenant_id: str) -> int:
        """Count a tenant's rows in one model.

        ``Any`` because these ORM classes share a ``tenant_id`` column but no
        common base declaring it; the call sites are a fixed literal list.
        """
        return int(
            session.execute(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            ).scalar_one()
        )
