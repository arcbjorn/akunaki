"""Internal debug surface: per-connection sync status.

**Unauthenticated.** This router is mounted only when
``AKUNAKI_DEBUG_ROUTES_ENABLED`` is explicitly true, and it serves tenant
connection status with no session check.

Health-data readback (``latest-sleep``) has been **removed**: it is fully
superseded by the authenticated ``/v1/sleep`` surface, so no unauthenticated
route serves health values anymore. Only ``sync-status`` remains as a local
diagnostic aid, and it has no ``/v1`` equivalent yet (a future authenticated
connection-status route would replace it).

Responses are marked ``private, no-store`` so status values are never cached.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
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
