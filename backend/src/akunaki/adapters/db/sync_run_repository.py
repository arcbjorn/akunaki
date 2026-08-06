"""Durable history of sync attempts.

``sync_runs`` has existed since the transport migration, with closed
vocabularies for trigger and status and an index on
``(connection_id, started_at)`` built for exactly this read — but **nothing ever
wrote a row**. ``raw_payloads.sync_run_id`` and ``raw_revisions.sync_run_id``
are FK columns meant to link ingested data back to the fetch that produced it,
and both were permanently NULL because no run id existed to reference.

The consequence was that a sync attempt left no trace. ``connection_health``
carries a failure *counter* and the last error class, so a user could see
"3 failures" but never when they happened, which stream, or whether the last
attempt even ran. A retry that fixed itself was indistinguishable from one that
never started.

Runs are opened before the fetch and closed with the outcome, so a crashed
worker leaves a ``running`` row rather than silently nothing — a visible
incomplete attempt is more honest than an absent one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import Connection, SyncRun
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.sync_runs import SyncRunStatus, SyncRunTrigger

__all__ = ["SyncRunRecord", "SyncRunRepository"]


@dataclass(frozen=True, slots=True)
class SyncRunRecord:
    """One recorded fetch execution.

    Carries no vendor body and no health values: an ``error_class`` is the
    typed failure label, never a provider message.
    """

    run_id: str
    connection_id: str
    provider: str
    trigger: str
    stream: str | None
    status: str
    started_at: str
    finished_at: str | None
    error_class: str | None
    new_revisions: int | None
    """Logical records this run ingested; None for a run that never settled."""


class SyncRunRepository:
    """Record and read sync attempts."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def open(
        self,
        *,
        run_id: str,
        tenant_id: str,
        connection_id: str,
        trigger: SyncRunTrigger,
        stream: str | None,
        now: datetime,
    ) -> str:
        """Record a run as ``running`` and return its id.

        Committed before the fetch begins rather than written once at the end:
        an attempt that dies mid-flight must still be visible, and a row only
        written on completion would lose exactly the failures worth seeing.
        """
        started_at = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            session.add(
                SyncRun(
                    id=run_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    trigger=trigger.value,
                    stream=stream,
                    status=SyncRunStatus.RUNNING.value,
                    started_at=started_at,
                    finished_at=None,
                    error_class=None,
                    stats_json=None,
                )
            )
        return run_id

    def close(
        self,
        *,
        run_id: str,
        status: SyncRunStatus,
        now: datetime,
        stats: Mapping[str, int] | None = None,
        error_class: str | None = None,
    ) -> bool:
        """Finish a run, returning whether a ``running`` row was closed.

        Guarded on ``status = 'running'`` so a late or duplicated close cannot
        rewrite a settled outcome — a retry that reuses an id must not turn a
        recorded failure into a success.

        ``stats`` is **counts only**, matching the schema's ``stats_json``
        column note. The type enforces it: an ``int``-valued mapping cannot
        smuggle a vendor string or a health value into a row the user reads.
        """
        if status is SyncRunStatus.RUNNING:
            msg = "close requires a terminal status"
            raise ValueError(msg)

        finished_at = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(SyncRun)
                .where(
                    SyncRun.id == run_id,
                    SyncRun.status == SyncRunStatus.RUNNING.value,
                )
                .values(
                    status=status.value,
                    finished_at=finished_at,
                    error_class=error_class,
                    stats_json=json.dumps(dict(stats), sort_keys=True)
                    if stats is not None
                    else None,
                )
            )
        return affected_rows(result) > 0

    def recent_for_tenant(
        self,
        *,
        tenant_id: str,
        limit: int,
    ) -> list[SyncRunRecord]:
        """The tenant's most recent runs, newest first.

        Ordered by ``started_at`` then ``id`` so runs sharing an instant have a
        stable order — without the tie-break, two runs opened in the same second
        could swap places between requests.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    SyncRun.id,
                    SyncRun.connection_id,
                    Connection.provider,
                    SyncRun.trigger,
                    SyncRun.stream,
                    SyncRun.status,
                    SyncRun.started_at,
                    SyncRun.finished_at,
                    SyncRun.error_class,
                    SyncRun.stats_json,
                )
                .join(Connection, Connection.id == SyncRun.connection_id)
                .where(SyncRun.tenant_id == tenant_id)
                .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
                .limit(limit)
            ).all()

        return [
            SyncRunRecord(
                run_id=row[0],
                connection_id=row[1],
                provider=row[2],
                trigger=row[3],
                stream=row[4],
                status=row[5],
                started_at=row[6],
                finished_at=row[7],
                error_class=row[8],
                new_revisions=_new_revisions(row[9]),
            )
            for row in rows
        ]


def _new_revisions(stats_json: str | None) -> int | None:
    """Read the ingest count out of a run's stats blob.

    Tolerant on read, because the count is supporting detail rather than the
    record itself: an unsettled run has no stats at all, and a build recording
    different counts must not make the whole listing fail. Malformed text is
    not among the cases — ``json_valid`` on ``stats_json`` rejects it at write
    time — so this only has to survive a *well-formed* blob without the key.
    """
    if not stats_json:
        return None
    stats = json.loads(stats_json)
    value = stats.get("new_revisions") if isinstance(stats, dict) else None
    return value if isinstance(value, int) else None
