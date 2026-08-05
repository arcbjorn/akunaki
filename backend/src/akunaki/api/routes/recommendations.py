"""``GET /v1/recommendations`` — the resolved guidance for one day, with losers.

Authenticated and tenant-scoped. ``/v1/today`` renders the primary and
supporting recommendations as part of a composite day view; this surface is the
recommendation set *itself*, and it additionally discloses the rules that fired
but were **suppressed**, each naming the rule that beat it.

That disclosure is the reason the endpoint exists. The engine resolves conflicts
by priority within a group and drops the losers, so "you were also over your
load target, but rest outranked it" is computed and then thrown away. Without
it a user cannot answer "why am I not being told to ease off?" — the honest
answer is that a higher-priority rule won, not that nothing fired.

Guidance only: wellness and performance, never diagnosis or treatment, and
never injury prediction. A rule id is a stable code, not a sentence — the client
owns the copy.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from akunaki.api.routes.today import today_service
from akunaki.api.security import CurrentSession
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.domain.recommendations import Recommendation

router = APIRouter(prefix="/v1/recommendations", tags=["recommendations"])


class RecommendationResponse(BaseModel):
    """One recommendation and how it placed in conflict resolution."""

    rule_id: str = Field(
        description=(
            "Stable rule code: sleep_extend_window, load_ease, rest_day, or "
            "data_gap_reconnect. Not display copy — the client owns wording."
        ),
    )
    role: str = Field(description="'primary', 'supporting', or 'suppressed'.")
    conflict_group: str = Field(description="'sleep', 'load', or 'data'.")
    suppressed_by: str | None = Field(
        default=None,
        description=("The rule id that outranked this one; null unless role is 'suppressed'."),
    )


class RecommendationsResponse(BaseModel):
    """The resolved recommendation set for one local health day."""

    local_health_day: str
    ruleset_version: str
    primary: RecommendationResponse | None = Field(
        description="At most one, by construction; null when no rule fired.",
    )
    supporting: list[RecommendationResponse]
    suppressed: list[RecommendationResponse] = Field(
        description=(
            "Rules that fired but lost their conflict group. Disclosed so a "
            "caller can explain why other guidance is absent."
        ),
    )


def _block(recommendation: Recommendation) -> RecommendationResponse:
    return RecommendationResponse(
        rule_id=recommendation.rule_id.value,
        role=recommendation.role.value,
        conflict_group=recommendation.conflict_group.value,
        suppressed_by=(
            recommendation.suppressed_by.value if recommendation.suppressed_by is not None else None
        ),
    )


@router.get("", response_model=RecommendationsResponse)
def read_recommendations(
    response: Response,
    session: CurrentSession,
    service: Annotated[TodaySurfaceService, Depends(today_service)],
    day: Annotated[
        str,
        Query(
            min_length=10,
            max_length=10,
            description="Local health day as YYYY-MM-DD.",
        ),
    ],
) -> RecommendationsResponse:
    """Return the resolved recommendation set for the caller's tenant and day.

    ``day`` is required rather than defaulted to the server's today: a local
    health day belongs to the tenant's timezone, and guessing it from the
    server's clock would silently answer for the wrong day.
    """
    response.headers["Cache-Control"] = "private, no-store"
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD"},
        ) from exc

    surface = service.today_for_day(tenant_id=session.tenant_id, local_health_day=day)

    return RecommendationsResponse(
        local_health_day=surface.local_health_day,
        ruleset_version=surface.ruleset_version,
        primary=(
            _block(surface.primary_recommendation)
            if surface.primary_recommendation is not None
            else None
        ),
        supporting=[_block(rec) for rec in surface.supporting_recommendations],
        suppressed=[_block(rec) for rec in surface.suppressed_recommendations],
    )
