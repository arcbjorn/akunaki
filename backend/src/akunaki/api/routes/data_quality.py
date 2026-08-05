"""``GET /v1/data-quality`` — standing conditions affecting the data.

Distinct from ``/v1/today``'s ``data_gaps``. A gap is a property of one day
("this day has no HRV") that resolves when data arrives. A finding is a
**standing** condition the user can usually act on: reconnect a provider, or
know that one has been silent for days.

Derived from current connection state, not stored. The design lists a
``data_quality_findings`` table but specifies no detector or resolution
lifecycle, and a persisted finding needs one — otherwise it outlives the
condition and tells the user to fix something already fixed.

Carries **no health values**: codes, severities, and provider names only. It
answers "is my data flowing", not "what does my data say".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.connections_surface import ConnectionsSurfaceService
from akunaki.domain.data_quality import ConnectionState, connection_findings
from akunaki.domain.jobs import parse_utc_rfc3339

router = APIRouter(prefix="/v1/data-quality", tags=["data-quality"])


class FindingResponse(BaseModel):
    """One standing condition affecting the data."""

    code: str = Field(
        description=(
            "Closed vocabulary: connection_needs_reauth, connection_revoked, "
            "connection_stale_sync, connection_never_synced, "
            "connection_repeated_failures, no_connections_linked."
        ),
    )
    severity: str = Field(description="'info', 'warning', or 'error'.")
    provider: str | None = Field(
        default=None, description="Which connector; null for tenant-wide findings."
    )


class DataQualityResponse(BaseModel):
    """The caller's standing data-quality findings, most severe first."""

    findings: list[FindingResponse]


@router.get("", response_model=DataQualityResponse)
def read_data_quality(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> DataQualityResponse:
    """Return the standing conditions affecting the caller's data.

    An empty list is the healthy answer — no finding means nothing needs
    attention, which is what most requests should return.
    """
    response.headers["Cache-Control"] = "private, no-store"
    summaries = ConnectionsSurfaceService(
        connections=ConnectionRepository(session_factory)
    ).connections_for_tenant(tenant_id=session.tenant_id)

    states = [
        ConnectionState(
            provider=summary.provider,
            status=summary.status,
            last_success_at=(
                parse_utc_rfc3339(summary.last_success_at)
                if summary.last_success_at is not None
                else None
            ),
            consecutive_failures=summary.consecutive_failures,
        )
        for summary in summaries
    ]
    findings = connection_findings(states, now=datetime.now(UTC))

    return DataQualityResponse(
        findings=[
            FindingResponse(
                code=finding.code.value,
                severity=finding.severity.value,
                provider=finding.provider,
            )
            for finding in findings
        ]
    )
