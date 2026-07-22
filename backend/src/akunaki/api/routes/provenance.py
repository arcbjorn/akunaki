"""The ``GET /v1/provenance/{token}`` opaque lineage surface.

A day response carries an opaque ``provenance_url`` (``/v1/provenance/<token>``)
instead of table or raw ids. This route resolves that token to the disclosed
lineage — the artifact kind, versions, status, freshness, and the roles of the
inputs — **never** an id of any stored row.

Authenticated and tenant-scoped: the token is resolved only within the caller's
tenant, and an unknown token is indistinguishable from a cross-tenant one (both
404), so a token cannot be probed for cross-tenant existence.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.derivation_repository import DerivationRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession

router = APIRouter(prefix="/v1/provenance", tags=["provenance"])


class ProvenanceInputResponse(BaseModel):
    """One disclosed lineage input — its role, never an id."""

    role: str


class ProvenanceResponse(BaseModel):
    """The disclosed lineage for a derived value.

    Carries versions, status, and freshness only — no table, raw, or run ids.
    """

    artifact_kind: str
    local_health_day: str | None
    formula_version: str
    status: str
    confidence: float | None
    freshness_at: str | None = Field(description="UTC RFC3339 when the value was derived.")
    as_of_at: str | None = Field(description="UTC RFC3339 evaluation instant.")
    inputs: list[ProvenanceInputResponse]


def _derivations(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> DerivationRepository:
    return DerivationRepository(session_factory)


@router.get("/{token}", response_model=ProvenanceResponse)
def provenance(
    token: str,
    response: Response,
    session: CurrentSession,
    derivations: Annotated[DerivationRepository, Depends(_derivations)],
) -> ProvenanceResponse:
    """Resolve an opaque provenance token to disclosed lineage for the caller."""
    response.headers["Cache-Control"] = "private, no-store"
    lineage = derivations.resolve_token(tenant_id=session.tenant_id, token=token)
    if lineage is None:
        # Unknown and cross-tenant are the same 404: a token cannot be probed.
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return ProvenanceResponse(
        artifact_kind=lineage.artifact_kind,
        local_health_day=lineage.local_health_day,
        formula_version=lineage.formula_version,
        status=lineage.status,
        confidence=lineage.confidence,
        freshness_at=lineage.freshness_at,
        as_of_at=lineage.as_of_at,
        inputs=[ProvenanceInputResponse(role=i.role) for i in lineage.inputs],
    )
