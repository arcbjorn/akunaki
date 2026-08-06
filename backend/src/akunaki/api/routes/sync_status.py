"""``GET /v1/sync/status`` — did my syncs actually run?

``/v1/connections`` answers *what is linked and is it healthy*: a status, the
last success, a failure counter. What it cannot answer is **history** — when the
failures happened, which stream, whether the last attempt even started. A
counter of 3 says nothing about whether the most recent attempt succeeded.

Reads ``sync_runs``, which existed since the transport migration but had no
writer until the recorder landed. A run is opened before its fetch and closed
with the outcome, so ``running`` here means an attempt that died mid-flight
rather than one still in progress — a worker that crashes leaves the row it
opened.

Carries **no health values and no vendor bodies**: an ``error_class`` is the
typed failure label only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.sync_run_repository import SyncRunRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession

router = APIRouter(prefix="/v1/sync", tags=["sync"])

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class SyncRunResponse(BaseModel):
    """One recorded sync attempt."""

    run_id: str
    connection_id: str
    provider: str
    trigger: str = Field(
        description="schedule, webhook, manual, reconcile, or initial.",
    )
    stream: str | None = Field(description="Which vendor stream was fetched.")
    status: str = Field(
        description=(
            "running, succeeded, failed, or partial. 'running' means the "
            "attempt never settled — a worker died mid-run."
        ),
    )
    started_at: str = Field(description="UTC RFC3339.")
    finished_at: str | None = Field(description="Null while a run is unsettled.")
    error_class: str | None = Field(
        description="Typed failure label only; never a vendor message.",
    )
    new_revisions: int | None = Field(
        default=None,
        description=(
            "Logical records this run ingested. Null for a run that never "
            "settled. Zero is meaningful: the fetch worked and nothing was new."
        ),
    )


class SyncStatusResponse(BaseModel):
    """The caller's recent sync attempts, newest first."""

    runs: list[SyncRunResponse]


@router.get("/status", response_model=SyncStatusResponse)
def read_sync_status(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="How many recent runs to return."),
    ] = DEFAULT_LIMIT,
) -> SyncStatusResponse:
    """Return the caller's recent sync attempts.

    An empty list is a real answer: a tenant that has never synced has no runs,
    which is not an error.
    """
    response.headers["Cache-Control"] = "private, no-store"
    runs = SyncRunRepository(session_factory).recent_for_tenant(
        tenant_id=session.tenant_id,
        limit=limit,
    )

    return SyncStatusResponse(
        runs=[
            SyncRunResponse(
                run_id=run.run_id,
                connection_id=run.connection_id,
                provider=run.provider,
                trigger=run.trigger,
                stream=run.stream,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error_class=run.error_class,
                new_revisions=run.new_revisions,
            )
            for run in runs
        ]
    )
