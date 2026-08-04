"""Read-only operational status for the readiness endpoint.

Reports what a deployment's readiness probe and dashboards need: the job-queue
depth by status, whether a leader currently holds the reaper lease, and whether
the database schema is at the code's migration head. All read-only — this
repository never writes, so hitting the readiness endpoint cannot perturb the
queue or leases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Job, LeaderLease, SystemCheck
from akunaki.domain.jobs import JobStatus, require_aware, to_utc_rfc3339


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


# Mirrors the CHECK on system_checks.detail.
_MAX_CHECK_DETAIL = 200


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


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The latest recorded result of one scheduled check."""

    name: str
    ok: bool
    detail: str | None
    checked_at: str


class SystemCheckRepository:
    """Persist and read the latest result of each scheduled system check."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        *,
        name: str,
        ok: bool,
        detail: str | None,
        now: datetime,
    ) -> None:
        """Overwrite the named check's latest result.

        Upsert rather than append: this is a latest-known-state cell, so a
        check that runs hourly must not accumulate rows a probe would scan.
        """
        if not name:
            msg = "name must be non-empty"
            raise ValueError(msg)
        if detail is not None and len(detail) > _MAX_CHECK_DETAIL:
            msg = f"detail must be at most {_MAX_CHECK_DETAIL} characters"
            raise ValueError(msg)

        checked_at = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            session.merge(
                SystemCheck(
                    name=name,
                    ok=1 if ok else 0,
                    detail=detail,
                    checked_at=checked_at,
                )
            )

    def latest(self, *, name: str) -> CheckResult | None:
        """Return the named check's latest result, or None when it never ran.

        None is meaningfully different from a failure: a check that has not run
        yet is unknown, not bad, and a probe must not report the two alike.
        """
        with self._session_factory() as session:
            row = session.get(SystemCheck, name)
            if row is None:
                return None
            return CheckResult(
                name=row.name,
                ok=bool(row.ok),
                detail=row.detail,
                checked_at=row.checked_at,
            )
