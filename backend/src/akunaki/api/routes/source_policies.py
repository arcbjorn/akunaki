"""``/v1/source-policies`` — how overlapping providers are resolved.

ADR 0005 requires source policies to be **inspectable**, and the product
principles require a user to be able to audit *why* a day looks the way it does.
The precedence was a private constant and the recorded decisions had no reader,
so neither was answerable.

Two reads:

- ``/effective`` — the precedence this build enforces, with its version.
- ``/decisions`` — what was actually chosen for one local day, and which
  providers competed.

Candidates are disclosed by **provider**, never by fact id: the user's question
is which source won and what else was available, and the provenance surface
already refuses to hand out row ids.

Overrides (``GET/PUT /v1/source-policies/override``) are **not** built: the
policy is code-defined in v0.1.0, so there is nothing a tenant could override.
Shipping a settable endpoint over a constant would imply a control that does
not exist.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.source_selection_repository import SourceSelectionReader
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.domain.source_policy import SLEEP_METRIC_FAMILY, effective_policy

router = APIRouter(prefix="/v1/source-policies", tags=["source-policies"])


class MetricFamilyPolicyResponse(BaseModel):
    """The precedence for one metric family, most authoritative first."""

    metric_family: str
    providers: list[str] = Field(
        description="Ordered most-authoritative first; a provider absent here never wins.",
    )


class EffectivePolicyResponse(BaseModel):
    """The source precedence actually in force."""

    policy_version: str
    families: list[MetricFamilyPolicyResponse] = Field(
        description="Only families with a real precedence rule; others are not enforced.",
    )


class CandidateResponse(BaseModel):
    """One provider that competed for a day."""

    provider: str
    rank: int = Field(description="Display order only — never a fallback order.")
    eligibility: str = Field(description="'eligible' or 'ineligible' under the policy.")
    reason: str


class SelectionResponse(BaseModel):
    """What was chosen for one local health day, and what else was available."""

    metric_family: str
    local_health_day: str
    selected_provider: str | None = Field(
        description="Null when no recognized source covered the day.",
    )
    selection_reason: str
    missing_reason: str | None
    policy_version: str
    version_n: int
    candidates: list[CandidateResponse]


@router.get("/effective", response_model=EffectivePolicyResponse)
def read_effective_policy(response: Response, session: CurrentSession) -> EffectivePolicyResponse:
    """Return the source precedence this build enforces.

    Authenticated but tenant-independent: the policy is code-defined, so every
    tenant gets the same answer. It is behind auth anyway because it describes
    internal engine behaviour, not public documentation.
    """
    response.headers["Cache-Control"] = "private, no-store"
    policy = effective_policy()
    return EffectivePolicyResponse(
        policy_version=policy.policy_version,
        families=[
            MetricFamilyPolicyResponse(
                metric_family=family.metric_family,
                providers=list(family.providers),
            )
            for family in policy.families
        ],
    )


@router.get("/decisions", response_model=SelectionResponse)
def read_decision(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    day: Annotated[
        str,
        Query(min_length=10, max_length=10, description="Local health day, YYYY-MM-DD."),
    ],
    metric_family: Annotated[
        str,
        Query(description="Which family's decision to read."),
    ] = SLEEP_METRIC_FAMILY,
) -> SelectionResponse:
    """Return the recorded source decision for one of the caller's days."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD"},
        ) from exc

    decision = SourceSelectionReader(session_factory).disclosed_selection(
        tenant_id=session.tenant_id,
        metric_family=metric_family,
        local_health_day=day,
    )
    if decision is None:
        # No decision recorded: the day has no facts for this family, or it
        # predates selection being recorded. Not an error, but there is nothing
        # to disclose, and inventing a default would misrepresent the engine.
        raise HTTPException(status_code=404, detail={"code": "no_decision"})

    return SelectionResponse(
        metric_family=decision.metric_family,
        local_health_day=decision.local_health_day,
        selected_provider=decision.selected_provider,
        selection_reason=decision.selection_reason,
        missing_reason=decision.missing_reason,
        policy_version=decision.policy_version,
        version_n=decision.version_n,
        candidates=[
            CandidateResponse(
                provider=candidate.provider,
                rank=candidate.rank,
                eligibility=candidate.eligibility,
                reason=candidate.reason,
            )
            for candidate in decision.candidates
        ],
    )
