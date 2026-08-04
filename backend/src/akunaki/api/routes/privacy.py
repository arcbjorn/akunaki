"""Privacy deletion routes: ``POST /v1/privacy/delete`` and its status read.

Authenticated and CSRF-enforced. The tenant comes from the validated session,
never a parameter — a caller can only erase **their own** tenant, and there is
no way to name someone else's.

Irreversible by design. The pipeline hard-deletes the tenant's health and
connection data, so the response is written after the erasure has actually
happened rather than acknowledging work that is merely queued.

The session dies with the tenant it authenticated: every session row is
tenant-scoped and cascades away with it. The delete response therefore also
clears the session cookie, so the browser is not left holding a credential that
can never authenticate again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.audit_repository import AuditRepository
from akunaki.adapters.db.deletion_repository import DeletionRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession, clear_session_cookie
from akunaki.application.deletion_service import DeletionService

router = APIRouter(prefix="/v1/privacy", tags=["privacy"])


class DeletionAcceptedResponse(BaseModel):
    """A completed deletion, described by counts only.

    Carries no identity and no health values: the same minimality rule the
    stored completion proof follows, since this is the last thing the caller
    ever receives for that tenant.
    """

    deletion_request_id: str = Field(
        description="Handle for the status read; outlives the tenant it erased.",
    )
    status: str
    rows_scrubbed: int = Field(description="Total rows hard-deleted across all classes.")
    jobs_cancelled: int


class DeletionStatusResponse(BaseModel):
    """Where a deletion request reached."""

    deletion_request_id: str
    status: str


@router.post("/delete", response_model=DeletionAcceptedResponse, status_code=200)
def start_deletion(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> DeletionAcceptedResponse:
    """Erase the caller's tenant. Irreversible.

    Runs the full pipeline before responding, so a 200 means the data is
    actually gone — not scheduled to go.
    """
    response.headers["Cache-Control"] = "private, no-store"
    service = DeletionService(
        pipeline=DeletionRepository(session_factory),
        new_id=lambda: str(uuid.uuid4()),
        audit=AuditRepository(session_factory),
    )
    outcome = service.delete_tenant(tenant_id=session.tenant_id, now=datetime.now(UTC))

    # The session cascaded away with its tenant; clear the cookie so the client
    # is not holding a credential that can never authenticate again.
    clear_session_cookie(response)
    return DeletionAcceptedResponse(
        deletion_request_id=outcome.request_id,
        status=outcome.status.value,
        rows_scrubbed=outcome.counts.total_rows,
        jobs_cancelled=outcome.counts.jobs_cancelled,
    )


@router.get("/delete/{deletion_request_id}", response_model=DeletionStatusResponse)
def deletion_status(
    deletion_request_id: str,
    response: Response,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> DeletionStatusResponse:
    """Report where a deletion request reached.

    **Unauthenticated by necessity**: the tenant — and every session that could
    authenticate for it — is gone by the time a completed request is worth
    reading, so requiring a session would make the status permanently
    unreachable.

    That is safe because the request id is an unguessable UUID and the response
    discloses only a pipeline status: no tenant id, no counts, no identity, and
    nothing that distinguishes an unknown id from another tenant's (both 404).
    """
    response.headers["Cache-Control"] = "private, no-store"
    status = DeletionRepository(session_factory).status_of(request_id=deletion_request_id)
    if status is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return DeletionStatusResponse(
        deletion_request_id=deletion_request_id,
        status=status.value,
    )
