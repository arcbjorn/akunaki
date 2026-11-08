"""Age-based retention for operational history: settled jobs and sync runs.

Both tables are bookkeeping that nothing ever deleted. On the production store
each grew by ~700 rows a day — one job and one run per stream per sweep tick —
and by 2026-11-08 the two, with ``job_attempts``, held 54 MB of a 349 MB file
and 59,000 rows apiece for a single user.

Both stores implement the retention sweep's ``purge_expired`` contract, so the
existing hourly job covers them with no new schedule:

- **Settled jobs** (``succeeded``, ``cancelled``) go after 30 days; their
  attempts, lease, and dead-letter rows go with them. ``failed`` and
  ``dead_letter`` jobs are the diagnostics and keep for 90. A job still
  ``ready`` or ``leased`` is never touched, whatever its age.
- **Sync runs** go after 90 days unless a payload or revision still cites them:
  the ``sync_run_id`` on retained data is its lineage back to the fetch, and
  that is never cut by housekeeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import (
    Job,
    JobAttempt,
    JobDeadLetter,
    JobLease,
    RawPayload,
    RawRevision,
    SyncRun,
)
from akunaki.domain.jobs import JobStatus, require_aware, to_utc_rfc3339
from akunaki.domain.sync_runs import SyncRunStatus

__all__ = ["SettledJobRetention", "SyncRunRetention"]

_SETTLED = (JobStatus.SUCCEEDED.value, JobStatus.CANCELLED.value)
_FAILED = (JobStatus.FAILED.value, JobStatus.DEAD_LETTER.value)


class SettledJobRetention:
    """Delete jobs that settled long enough ago, with their child rows."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        keep_settled: timedelta = timedelta(days=30),
        keep_failed: timedelta = timedelta(days=90),
    ) -> None:
        self._session_factory = session_factory
        self._keep_settled = keep_settled
        self._keep_failed = keep_failed

    def purge_expired(self, *, now: datetime) -> int:
        """Delete settled jobs past their retention; return how many went."""
        aware = require_aware(now, field_name="now")
        settled_cutoff = to_utc_rfc3339(aware - self._keep_settled)
        failed_cutoff = to_utc_rfc3339(aware - self._keep_failed)
        doomed = (Job.status.in_(_SETTLED) & (Job.updated_at < settled_cutoff)) | (
            Job.status.in_(_FAILED) & (Job.updated_at < failed_cutoff)
        )
        doomed_ids = select(Job.id).where(doomed)

        with self._session_factory() as session, session.begin():
            # Children first. The schema cascades, but an explicit delete does
            # not depend on the connection having foreign keys enabled.
            for child in (JobAttempt, JobDeadLetter, JobLease):
                session.execute(delete(child).where(child.job_id.in_(doomed_ids)))
            result = session.execute(delete(Job).where(doomed))
            return affected_rows(result)


class SyncRunRetention:
    """Delete old sync runs that no retained payload or revision cites."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        keep: timedelta = timedelta(days=90),
    ) -> None:
        self._session_factory = session_factory
        self._keep = keep

    def purge_expired(self, *, now: datetime) -> int:
        """Delete uncited sync runs older than the retention; return how many went."""
        cutoff = to_utc_rfc3339(require_aware(now, field_name="now") - self._keep)
        # ``NOT IN`` against a set containing NULL matches nothing, so the
        # citing columns are filtered to non-null before the comparison.
        cited_by_payload = select(RawPayload.sync_run_id).where(RawPayload.sync_run_id.is_not(None))
        cited_by_revision = select(RawRevision.sync_run_id).where(
            RawRevision.sync_run_id.is_not(None)
        )

        with self._session_factory() as session, session.begin():
            result = session.execute(
                delete(SyncRun).where(
                    SyncRun.started_at < cutoff,
                    SyncRun.status != SyncRunStatus.RUNNING.value,
                    SyncRun.id.not_in(cited_by_payload),
                    SyncRun.id.not_in(cited_by_revision),
                )
            )
            return affected_rows(result)
