"""The ``/v1/workouts`` surface: session list and detail.

Authenticated and tenant-scoped — the tenant comes from the validated session,
never a parameter, and an unknown id is indistinguishable from another tenant's.

Discloses what was **measured** (start/end, per-zone minutes) plus the canonical
``session_load`` computed internally from those minutes. There is no workout
score: v0.1.0 ships exactly one score code (recovery), and adding a second here
would imply a formula that has not been accepted.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.workouts_surface import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    WorkoutsSurfaceService,
    WorkoutSummary,
)

router = APIRouter(prefix="/v1/workouts", tags=["workouts"])


class WorkoutResponse(BaseModel):
    """One workout session.

    ``session_load`` is this system's canonical load, computed from the zone
    minutes below — never a vendor-supplied training-load field.
    """

    workout_id: str
    provider: str
    local_health_day: str
    start_utc: str
    end_utc: str
    session_load: float = Field(description="Canonical zone-weighted load, computed internally.")
    zone1_min: float
    zone2_min: float
    zone3_min: float
    zone4_min: float
    zone5_min: float
    total_zone_min: float = Field(description="Sum of the five zone minutes.")


class WorkoutsResponse(BaseModel):
    """One page of workouts, newest first."""

    items: list[WorkoutResponse]
    next_cursor: str | None = Field(
        description="Opaque handle for the next page; null when this is the last.",
    )


def _to_response(summary: WorkoutSummary) -> WorkoutResponse:
    return WorkoutResponse(
        workout_id=summary.workout_id,
        provider=summary.provider,
        local_health_day=summary.local_health_day,
        start_utc=summary.start_utc,
        end_utc=summary.end_utc,
        session_load=summary.session_load,
        zone1_min=summary.zone1_min,
        zone2_min=summary.zone2_min,
        zone3_min=summary.zone3_min,
        zone4_min=summary.zone4_min,
        zone5_min=summary.zone5_min,
        total_zone_min=summary.total_zone_min,
    )


@router.get("", response_model=WorkoutsResponse)
def list_workouts(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description="Page size."),
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from a previous page's next_cursor."),
    ] = None,
) -> WorkoutsResponse:
    """List the caller's workouts, newest first."""
    response.headers["Cache-Control"] = "private, no-store"
    service = WorkoutsSurfaceService(workouts=FactRepository(session_factory))
    try:
        page = service.list_for_tenant(
            tenant_id=session.tenant_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        # A cursor the client did not get from us: a request error, not a
        # reason to silently restart from the newest page.
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_cursor", "message": "cursor is not valid"},
        ) from exc

    return WorkoutsResponse(
        items=[_to_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{workout_id}", response_model=WorkoutResponse)
def get_workout(
    workout_id: str,
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> WorkoutResponse:
    """Return one of the caller's workouts."""
    response.headers["Cache-Control"] = "private, no-store"
    service = WorkoutsSurfaceService(workouts=FactRepository(session_factory))
    summary = service.get_for_tenant(tenant_id=session.tenant_id, workout_id=workout_id)
    if summary is None:
        # Unknown and cross-tenant are the same 404: an id cannot be probed.
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return _to_response(summary)
