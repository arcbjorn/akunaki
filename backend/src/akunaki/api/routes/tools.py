"""The ``/v1/tools`` registry surface: list and invoke typed tools over REST.

This is the "tools usable by REST without model packages" phase-two exit
criterion in the flesh: the same typed registry an agent or MCP adapter would
use is exposed to a plain HTTP client. Every tool runs under the caller's
session context, so a tool can no more cross tenants than a direct route.

Only ``side_effect=none`` read tools are reachable here for now; a mutating tool
would additionally require the confirmation flow before it could execute.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.score_repository import ScoreRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import RecoverySurfaceService, ServedRecoveryService
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.application.tool_registry import ToolContext, ToolNotFoundError, ToolRegistry
from akunaki.application.tools.health import register_health_tools

router = APIRouter(prefix="/v1/tools", tags=["tools"])


class ToolMetadata(BaseModel):
    """A tool's declared contract and metadata."""

    name: str
    version: str
    scopes: list[str]
    sensitivity: str
    side_effect: str
    model_exposure: bool
    requires_confirmation: bool


class ToolListResponse(BaseModel):
    """The registered tools available to the caller."""

    tools: list[ToolMetadata]


class ToolInvokeRequest(BaseModel):
    """A request to run a tool by name with typed arguments."""

    input: dict[str, Any] = Field(default_factory=dict, description="Tool input arguments.")


def _registry(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ToolRegistry:
    facts = FactRepository(session_factory)
    inputs = RecoveryInputService(features=facts, subjective=CheckInRepository(session_factory))
    compute = RecoverySurfaceService(inputs=inputs)
    served = ServedRecoveryService(stored=ScoreRepository(session_factory), compute=compute)
    today = TodaySurfaceService(
        recovery=served,
        sleep=SleepSurfaceService(durations=facts),
        anomalies=AnomalyRepository(session_factory),
    )
    registry = ToolRegistry()
    register_health_tools(
        registry,
        recovery=served,
        sleep=SleepSurfaceService(durations=facts),
        today=today,
    )
    return registry


RegistryDep = Annotated[ToolRegistry, Depends(_registry)]


@router.get("", response_model=ToolListResponse)
def list_tools(
    response: Response, session: CurrentSession, registry: RegistryDep
) -> ToolListResponse:
    """List the registered tools and their metadata."""
    response.headers["Cache-Control"] = "private, no-store"
    tools = [
        ToolMetadata(
            name=name,
            version=(tool := registry.get(name)).version,
            scopes=list(tool.scopes),
            sensitivity=tool.sensitivity.value,
            side_effect=tool.side_effect.value,
            model_exposure=tool.model_exposure,
            requires_confirmation=tool.requires_confirmation,
        )
        for name in registry.names()
    ]
    return ToolListResponse(tools=tools)


@router.post("/{tool_name}")
def invoke_tool(
    tool_name: str,
    response: Response,
    session: CurrentSession,
    registry: RegistryDep,
    body: ToolInvokeRequest,
) -> dict[str, Any]:
    """Invoke a read tool by name under the caller's session context."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        tool = registry.get(tool_name)
    except ToolNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "tool_not_found", "name": tool_name}
        ) from exc

    context = ToolContext(tenant_id=session.tenant_id, user_id=session.user_id)
    try:
        result = tool.invoke(body.input, context)
    except ValueError as exc:
        # Input validation or a bad day argument: a client error, not a 500.
        raise HTTPException(
            status_code=422, detail={"code": "invalid_tool_input", "message": str(exc)}
        ) from exc
    return result.model_dump()
