"""The ``POST /v1/checkin`` subjective check-in write surface.

The first authenticated **write** path. Cookie-authenticated and tenant-scoped;
because it is a state-changing method, ``require_session`` enforces the CSRF
header automatically. The tenant comes from the validated session, never the
body.

A check-in records the day's normalized energy, stress, and symptom-burden on
[0, 1]. All three are required for the subjective recovery component to be
computed; the write itself is versioned (a re-submission for the same day
supersedes the prior one).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.domain.subjective import SubjectiveInputs

router = APIRouter(prefix="/v1/checkin", tags=["checkin"])


class CheckInRequest(BaseModel):
    """A completed daily check-in. All three fields are normalized to [0, 1]."""

    local_health_day: str = Field(min_length=10, max_length=10, description="YYYY-MM-DD.")
    energy_n: float = Field(ge=0.0, le=1.0, description="Higher is better.")
    stress_n: float = Field(ge=0.0, le=1.0, description="Higher is worse.")
    symptom_burden_n: float = Field(
        ge=0.0,
        le=1.0,
        description="Higher is worse; 0 means explicitly no symptoms.",
    )


class CheckInResponse(BaseModel):
    """The recorded check-in's identity and version."""

    check_in_id: str
    local_health_day: str
    version_n: int


def _check_ins(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> CheckInRepository:
    return CheckInRepository(session_factory)


@router.post("", response_model=CheckInResponse)
def record_check_in(
    response: Response,
    session: CurrentSession,
    body: CheckInRequest,
    check_ins: Annotated[CheckInRepository, Depends(_check_ins)],
) -> CheckInResponse:
    """Record the caller's completed check-in for a day (tenant from session)."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        date.fromisoformat(body.local_health_day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "local_health_day must be YYYY-MM-DD"},
        ) from exc

    now = datetime.now(UTC)
    outcome = check_ins.record_check_in(
        check_in_id=str(uuid.uuid4()),
        tenant_id=session.tenant_id,
        local_health_day=body.local_health_day,
        inputs=SubjectiveInputs(
            energy_n=body.energy_n,
            stress_n=body.stress_n,
            symptom_burden_n=body.symptom_burden_n,
        ),
        completed_at=now,
        now=now,
    )
    return CheckInResponse(
        check_in_id=outcome.check_in_id,
        local_health_day=body.local_health_day,
        version_n=outcome.version_n,
    )
