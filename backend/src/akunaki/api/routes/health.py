"""Liveness/readiness-style health endpoint (core foundation only)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["health"])


class HealthzResponse(BaseModel):
    """Typed health response. Does not fabricate product health data."""

    status: Literal["ok", "degraded"] = Field(
        description="ok when the service process is up; degraded if DB is not ready.",
    )
    service: str = Field(description="Configured service name.")
    database_ready: bool = Field(description="Result of a simple SELECT 1 probe.")
    models_required: Literal[False] = Field(
        default=False,
        description="Core API never requires model providers.",
    )


@router.get("/healthz", response_model=HealthzResponse)
def healthz(request: Request) -> HealthzResponse:
    """Report process status and database readiness only."""
    settings = request.app.state.settings
    probe = request.app.state.probe_database_ready
    database_ready = bool(probe())
    status: Literal["ok", "degraded"] = "ok" if database_ready else "degraded"
    return HealthzResponse(
        status=status,
        service=settings.service_name,
        database_ready=database_ready,
        models_required=False,
    )
