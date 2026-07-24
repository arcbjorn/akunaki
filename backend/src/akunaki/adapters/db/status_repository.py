"""Read-only operational status for the readiness endpoint.

Reports what a deployment's readiness probe and dashboards need: the job-queue
depth by status, whether a leader currently holds the reaper lease, and whether
the database schema is at the code's migration head. All read-only — this
repository never writes, so hitting the readiness endpoint cannot perturb the
queue or leases.
"""

from __future__ import annotations

from dataclasses import dataclass

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Job, LeaderLease
from akunaki.domain.jobs import JobStatus


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """Job counts by lifecycle status."""

    ready: int
    leased: int
    dead_letter: int


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    """Whether the DB schema matches the code's migration head."""

    at_head: bool
    db_revision: str | None
    code_head: str | None


class OperationalStatusRepository:
    """Read operational counters, leader state, and migration status."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def queue_snapshot(self) -> QueueSnapshot:
        """Return job counts for the operationally interesting statuses."""
        with self._session_factory() as session:
            rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        counts = {status: int(n) for status, n in rows}
        return QueueSnapshot(
            ready=counts.get(JobStatus.READY.value, 0),
            leased=counts.get(JobStatus.LEASED.value, 0),
            dead_letter=counts.get(JobStatus.DEAD_LETTER.value, 0),
        )

    def leader_held(self, *, lease_name: str) -> bool:
        """Whether the named leader lease is currently owned by some worker.

        A held lease means a worker is coordinating reaping/scheduling. Absence
        is not an error — it just means no worker currently leads (e.g. between
        deploys) — so this is reported, not asserted.
        """
        with self._session_factory() as session:
            owner = session.execute(
                select(LeaderLease.lease_owner).where(LeaderLease.lease_name == lease_name)
            ).scalar_one_or_none()
        return owner is not None


def migration_status(engine: Engine, *, config: Config) -> MigrationStatus:
    """Compare the DB's applied revision to the code's migration head.

    ``at_head`` is True only when the database is at exactly the code head, so a
    deployment whose migrations have not been run reads as not-ready without
    guessing.
    """
    script = ScriptDirectory.from_config(config)
    code_head = script.get_current_head()
    with engine.connect() as conn:
        db_revision = MigrationContext.configure(conn).get_current_revision()
    return MigrationStatus(
        at_head=db_revision is not None and db_revision == code_head,
        db_revision=db_revision,
        code_head=code_head,
    )
