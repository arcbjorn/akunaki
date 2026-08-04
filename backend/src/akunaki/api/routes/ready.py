"""Readiness endpoint: deeper operational status for probes and dashboards.

``/healthz`` stays a cheap liveness check (process up, DB reachable). ``/readyz``
answers "is this deployment actually able to do work?": the schema is at the
migration head, the queue is not backing up or dead-lettering, and a worker
currently leads. It is read-only, so a probe hitting it never perturbs state.

A deployment is **ready** only when the database is reachable and at the code's
migration head. Queue depth and leader presence are **reported** (a dashboard
signal), not gating — an idle queue or a momentary leaderless gap between
deploys is not an unready service.
"""

from __future__ import annotations

from typing import Annotated

from alembic.config import Config
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.audit_repository import AuditRepository
from akunaki.adapters.db.status_repository import (
    OperationalStatusRepository,
    migration_status,
)
from akunaki.api.app import get_engine, get_session_factory
from akunaki.migrations import script_location

# The reaper/scheduler leader lease name; a held lease means a worker leads.
_REAPER_LEASE_NAME = "core-reaper"

router = APIRouter(tags=["health"])


class QueueBlock(BaseModel):
    """Job counts for the operationally interesting statuses."""

    ready: int
    leased: int
    dead_letter: int


class MigrationBlock(BaseModel):
    """Whether the DB schema is at the code's migration head."""

    at_head: bool
    db_revision: str | None
    code_head: str | None


class AuditBlock(BaseModel):
    """How current the audit trail is.

    Reported, never gating: an empty trail is normal on a fresh deployment, and
    a quiet one only means no audited action happened. The value is
    ``last_event_at`` going stale while audited actions are still occurring —
    that means the trail stopped being written, which no counter would show.

    Deliberately **not** a verification verdict: verifying is O(chain), so a
    probe must never trigger it. The scheduled ``audit.verify_chain`` job owns
    that and publishes its result as a worker-process gauge.
    """

    events: int = Field(description="Total events recorded (the chain's length).")
    last_event_at: str | None = Field(
        default=None, description="UTC RFC3339 of the newest event; null when empty."
    )


class ReadyzResponse(BaseModel):
    """Readiness detail. ``ready`` gates on DB reachable + at migration head."""

    ready: bool = Field(description="True when the DB is reachable and at head.")
    database_ready: bool
    migration: MigrationBlock
    queue: QueueBlock
    audit: AuditBlock
    leader_held: bool = Field(
        description="Whether a worker currently holds the reaper/scheduler lease."
    )


def _alembic_config() -> Config:
    """Build a minimal alembic config pointing at the packaged migrations.

    The script location is resolved by importing the migrations package rather
    than walking parent directories, so this works from a source checkout and
    an installed wheel alike. Reading the head needs only the script directory,
    not ``alembic.ini`` — so no config file is loaded, and a deployment that
    does not ship one still reports readiness instead of raising.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location()))
    return cfg


@router.get("/readyz", response_model=ReadyzResponse)
def readyz(
    request: Request,
    response: Response,
    engine: Annotated[Engine, Depends(get_engine)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ReadyzResponse:
    """Report readiness: schema head, queue depth, leader presence."""
    response.headers["Cache-Control"] = "no-store"

    database_ready = bool(request.app.state.probe_database_ready())

    # A down DB cannot answer schema/queue questions; report the unknowns rather
    # than raising, and gate readiness on the DB being reachable and at head.
    at_head = False
    db_revision: str | None = None
    code_head: str | None = None
    queue = QueueBlock(ready=0, leased=0, dead_letter=0)
    audit = AuditBlock(events=0, last_event_at=None)
    leader_held = False

    if database_ready:
        status = OperationalStatusRepository(session_factory)
        migration = migration_status(engine, config=_alembic_config())
        at_head = migration.at_head
        db_revision = migration.db_revision
        code_head = migration.code_head
        snapshot = status.queue_snapshot()
        queue = QueueBlock(
            ready=snapshot.ready, leased=snapshot.leased, dead_letter=snapshot.dead_letter
        )
        leader_held = status.leader_held(lease_name=_REAPER_LEASE_NAME)
        if at_head:
            # Only once the schema matches the code: a DB behind head may not
            # have `audit_events` yet, and a probe whose job is to *report*
            # that must not itself fail on the missing table.
            tail = AuditRepository(session_factory).tail()
            audit = AuditBlock(
                events=tail[0] if tail is not None else 0,
                last_event_at=tail[1] if tail is not None else None,
            )

    ready = database_ready and at_head
    if not ready:
        response.status_code = 503

    return ReadyzResponse(
        ready=ready,
        database_ready=database_ready,
        migration=MigrationBlock(at_head=at_head, db_revision=db_revision, code_head=code_head),
        queue=queue,
        audit=audit,
        leader_held=leader_held,
    )


__all__ = ["router"]
