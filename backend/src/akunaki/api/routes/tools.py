"""The ``/v1/tools`` registry surface: list and invoke typed tools over REST.

This is the "tools usable by REST without model packages" phase-two exit
criterion in the flesh: the same typed registry an agent or MCP adapter would
use is exposed to a plain HTTP client. Every tool runs under the caller's
session context, so a tool can no more cross tenants than a direct route.

Read tools execute directly. ``connections.sync`` is the one mutating tool: it
declares ``requires_confirmation``, which is enforced for calls carrying a
``run_id`` (the canonical registry's "yes if agent") — a direct session call is
already an explicit, CSRF-enforced human act.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.adapters.db.confirmation_repository import ConfirmationRepository
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.score_repository import ScoreRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.anomalies_surface import AnomaliesSurfaceService
from akunaki.application.connections_surface import ConnectionsSurfaceService
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import RecoverySurfaceService, ServedRecoveryService
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.application.sync_request import SyncRequestService
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.application.tool_registry import (
    Tool,
    ToolContext,
    ToolNotFoundError,
    ToolRegistry,
)
from akunaki.application.tools.connections import register_connection_tools
from akunaki.application.tools.health import register_health_tools
from akunaki.application.workouts_surface import WorkoutsSurfaceService
from akunaki.domain.confirmations import ConfirmationBinding, canonical_args_hash
from akunaki.domain.sessions import AuthenticatedSession

logger = logging.getLogger("akunaki.tools")

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
    confirmation_token: str | None = Field(
        default=None,
        description="Confirmation authorizing this call; required for mutating tools.",
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Part of the confirmation binding; required for mutating tools.",
    )
    run_id: str | None = Field(
        default=None,
        description="Agent conversation run this call belongs to, when any.",
    )


def _confirmations(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ConfirmationRepository:
    return ConfirmationRepository(session_factory)


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
        anomalies=AnomaliesSurfaceService(anomalies=AnomalyRepository(session_factory)),
        workouts=WorkoutsSurfaceService(workouts=facts),
    )
    register_connection_tools(
        registry,
        connections=ConnectionsSurfaceService(connections=ConnectionRepository(session_factory)),
        sync=SyncRequestService(
            connections=ConnectionRepository(session_factory),
            jobs=JobRepository(session_factory),
            new_id=lambda: str(uuid.uuid4()),
        ),
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


def _require_confirmation(
    *,
    tool: Tool[Any, Any],
    body: ToolInvokeRequest,
    session: AuthenticatedSession,
    confirmations: ConfirmationRepository,
) -> None:
    """Redeem the confirmation authorizing this exact call, or refuse it.

    **Fails closed.** A tool declaring ``requires_confirmation`` executes only
    with a confirmation that matches the full binding; a missing token is a
    refusal, not a bypass.

    Every rejection is the same generic 403. Distinguishing "expired" from
    "wrong tool" would let a caller probe for valid tool names, run ids, or
    live tokens.
    """
    if not body.confirmation_token or not body.idempotency_key:
        raise HTTPException(
            status_code=403,
            detail={"code": "confirmation_required", "tool": tool.name},
        )

    requested = ConfirmationBinding(
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        run_id=body.run_id,
        tool_name=tool.name,
        args_hash=canonical_args_hash(body.input),
        idempotency_key=body.idempotency_key,
    )
    rejection = confirmations.consume(
        token=body.confirmation_token,
        requested=requested,
        now=datetime.now(UTC),
    )
    if rejection is not None:
        logger.warning(
            "tool confirmation rejected",
            extra={"tool": tool.name, "reason": rejection.value},
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "confirmation_invalid", "tool": tool.name},
        )


@router.post("/{tool_name}")
def invoke_tool(
    tool_name: str,
    response: Response,
    session: CurrentSession,
    registry: RegistryDep,
    body: ToolInvokeRequest,
    confirmations: Annotated[ConfirmationRepository, Depends(_confirmations)],
) -> dict[str, Any]:
    """Invoke a read tool by name under the caller's session context."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        tool = registry.get(tool_name)
    except ToolNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "tool_not_found", "name": tool_name}
        ) from exc

    # "Confirmation required **if agent**" (canonical registry): a call carrying
    # a run_id originates in an agent run and must prove the user authorized
    # that exact call. A direct session call is already an explicit human act —
    # it is CSRF-enforced and tenant-scoped — so it needs no second approval.
    if tool.requires_confirmation and body.run_id is not None:
        _require_confirmation(
            tool=tool,
            body=body,
            session=session,
            confirmations=confirmations,
        )

    context = ToolContext(tenant_id=session.tenant_id, user_id=session.user_id)
    try:
        result = tool.invoke(body.input, context)
    except ValueError as exc:
        # Input validation or a bad day argument: a client error, not a 500.
        raise HTTPException(
            status_code=422, detail={"code": "invalid_tool_input", "message": str(exc)}
        ) from exc
    except LookupError as exc:
        # The tool ran but its subject does not exist for this tenant. The
        # message is generic on purpose: unknown and cross-tenant must be
        # indistinguishable, so an id cannot be probed through a tool either.
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from exc
    return result.model_dump()
