"""Internal debug surface: sync status and latest sleep fact.

**Unauthenticated.** This router is mounted only when
``AKUNAKI_DEBUG_ROUTES_ENABLED`` is explicitly true, and it serves tenant
health data with no session check. It exists to satisfy phase one's vertical
slice ("see raw sync success and latest sleep fact in API — internal/debug")
and must be replaced by authenticated ``/v1`` routes once sessions exist.

Responses are marked ``private, no-store`` so health values are never cached,
matching the caching rule for authenticated health responses.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.debug_queries import DebugQueries
from akunaki.api.app import get_session_factory

router = APIRouter(prefix="/internal/debug", tags=["debug"])


class ConnectionSyncStatusResponse(BaseModel):
    """Sync progress for one connection."""

    connection_id: str
    provider: str
    status: str = Field(description="pending, active, needs_reauth, revoked, or error.")
    last_success_at: str | None
    last_error_class: str | None = Field(
        description="Error class only; never a vendor body or message.",
    )
    consecutive_failures: int
    transport_pages: int = Field(description="Raw vendor responses retained.")
    raw_revisions: int = Field(description="Logical records ingested for the tenant.")


class SyncStatusResponse(BaseModel):
    """Per-connection sync status for one tenant."""

    tenant_id: str
    connections: list[ConnectionSyncStatusResponse]


class LatestSleepFactResponse(BaseModel):
    """The most recent current sleep fact, with its lineage."""

    fact_record_id: str
    local_health_day: str | None = Field(description="Wake-date bucket, YYYY-MM-DD.")
    start_utc: str | None
    end_utc: str | None
    duration_min: float
    time_in_bed_min: float | None
    efficiency_pct: float | None
    is_nap: bool
    quality: str = Field(description="high, medium, low, or unknown.")
    confidence: float
    normalizer_version: str
    raw_revision_id: str | None = Field(description="Lineage back to the raw record.")
    version_n: int


def _queries(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> DebugQueries:
    return DebugQueries(session_factory)


@router.get("/sync-status", response_model=SyncStatusResponse)
def sync_status(
    response: Response,
    queries: Annotated[DebugQueries, Depends(_queries)],
    tenant_id: Annotated[str, Query(min_length=1)],
) -> SyncStatusResponse:
    """Report whether a tenant's connections have synced."""
    response.headers["Cache-Control"] = "private, no-store"
    return SyncStatusResponse(
        tenant_id=tenant_id,
        connections=[
            ConnectionSyncStatusResponse(**asdict(status))
            for status in queries.sync_status(tenant_id=tenant_id)
        ],
    )


@router.get("/latest-sleep", response_model=LatestSleepFactResponse)
def latest_sleep(
    response: Response,
    queries: Annotated[DebugQueries, Depends(_queries)],
    tenant_id: Annotated[str, Query(min_length=1)],
) -> LatestSleepFactResponse:
    """Return the tenant's most recent current sleep fact."""
    response.headers["Cache-Control"] = "private, no-store"
    fact = queries.latest_sleep_fact(tenant_id=tenant_id)
    if fact is None:
        # 404 rather than an empty body: "no fact yet" and "no such tenant" are
        # deliberately indistinguishable, matching the cross-tenant 404 rule.
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return LatestSleepFactResponse(**asdict(fact))
