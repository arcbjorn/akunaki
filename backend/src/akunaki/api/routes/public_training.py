"""``GET /v1/public/training`` — an unauthenticated 30-day training calendar.

Mounted only when ``AKUNAKI_PUBLIC_TRAINING_TENANT_ID`` names a tenant, and
serves that tenant alone. It exists so a personal public page can show daily
training consistency straight from the system of record instead of a hand-kept
list, and it discloses exactly what such a page needs: per local day, trained
or not — no times, zones, loads, counts, or any other measurement.

Everything about it is the opposite of the ``/v1`` session surfaces on
purpose: no cookie, ``Cache-Control: public`` so an edge cache absorbs the
traffic, ``Access-Control-Allow-Origin: *`` because the body is public and
uncredentialed (the CORS allow-list stays for the credentialed PWA routes),
and a relaxed ``Cross-Origin-Resource-Policy`` so a browser on another origin
may read it.

"Today" is the tenant's local date under its stated ``primary_timezone`` —
there is no caller to name a day, so the tenant's own preference decides.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.user_repository import UserRepository
from akunaki.api.app import get_session_factory
from akunaki.application.public_training_surface import (
    PublicTrainingCalendar,
    PublicTrainingSurfaceService,
)
from akunaki.config import Settings

router = APIRouter(prefix="/v1/public", tags=["public"])

# One hour: a day flips at most once a day and a sync lands a session within
# the reconcile interval anyway, so a stale hour is invisible on a dot calendar.
_CACHE_CONTROL = "public, max-age=3600"


class PublicTrainingDayResponse(BaseModel):
    """One local day of the calendar."""

    day: str = Field(description="Local health day, YYYY-MM-DD.")
    trained: bool


class PublicTrainingResponse(BaseModel):
    """The public 30-day calendar. Deliberately carries no measurement."""

    as_of: str = Field(description="The tenant's local today; last day of the window.")
    window_days: int
    days: list[PublicTrainingDayResponse] = Field(description="Oldest first.")
    sources: list[str] = Field(description="Providers that recorded a session in the window.")
    definition: str = Field(description="What makes a day count, stated verbatim.")


def _to_response(calendar: PublicTrainingCalendar) -> PublicTrainingResponse:
    return PublicTrainingResponse(
        as_of=calendar.as_of,
        window_days=calendar.window_days,
        days=[
            PublicTrainingDayResponse(day=entry.local_health_day, trained=entry.trained)
            for entry in calendar.days
        ],
        sources=list(calendar.sources),
        definition=calendar.definition,
    )


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _local_today(timezone_name: str, now: datetime) -> date:
    """The calendar date at ``now`` in the tenant's timezone; UTC if it is unknown.

    A tenant row can only hold what the code wrote (``UTC`` at provisioning),
    but a hand-edited or future value must not take the public page down.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return now.astimezone(zone).date()


@router.get("/training", response_model=PublicTrainingResponse)
def public_training(
    response: Response,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    settings: Annotated[Settings, Depends(_settings)],
) -> PublicTrainingResponse:
    """The configured tenant's last 30 local days: trained or not."""
    tenant_id = settings.public_training_tenant_id.strip()
    timezone_name = UserRepository(session_factory).tenant_timezone(tenant_id=tenant_id)
    if timezone_name is None:
        # The operator named a tenant that does not exist (or was scrubbed).
        # An empty calendar here would be a fabricated "never trains"; say
        # instead that the surface is not provisioned.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "public_training_unavailable",
                "message": "public training tenant is not provisioned",
            },
        )

    today = _local_today(timezone_name, datetime.now(UTC))
    service = PublicTrainingSurfaceService(source=FactRepository(session_factory))
    calendar = service.calendar(tenant_id=tenant_id, today=today)

    response.headers["Cache-Control"] = _CACHE_CONTROL
    # Public and uncredentialed: any origin may read it, and the security
    # middleware's same-origin resource policy is relaxed for this route only.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return _to_response(calendar)


__all__ = ["router"]
