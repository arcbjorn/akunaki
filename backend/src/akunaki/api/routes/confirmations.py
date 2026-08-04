"""``POST /v1/confirmations`` — authorize one specific mutating tool call.

The design requires the user to confirm **out-of-band via API, not the model**.
This is that endpoint: a session-authenticated human names the tool and the
exact arguments they are approving, and receives a one-time token bound to that
call.

Authenticated and CSRF-enforced. The binding's ``tenant_id`` and ``user_id``
come from the session, never the request, so a confirmation can only ever
authorize a call by the person who granted it.

The returned token is shown **once**. Only its SHA-256 is stored, so it cannot
be recovered from the database — losing it means requesting another, which is
the correct failure mode for an authorization handle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.sessions import generate_confirmation_token
from akunaki.adapters.db.confirmation_repository import ConfirmationRepository
from akunaki.api.app import get_session_factory
from akunaki.api.routes.tools import RegistryDep
from akunaki.api.security import CurrentSession
from akunaki.application.tool_registry import ToolNotFoundError
from akunaki.domain.confirmations import ConfirmationBinding, canonical_args_hash

router = APIRouter(prefix="/v1/confirmations", tags=["confirmations"])

# Short enough that an abandoned approval cannot be spent much later, long
# enough for a person to read a confirmation dialog and decide.
CONFIRMATION_TTL = timedelta(minutes=5)


class ConfirmationRequest(BaseModel):
    """The exact call the user is approving."""

    tool_name: str = Field(min_length=1)
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="The arguments being approved; hashed into the binding.",
    )
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        description="Part of the binding; the same key must be replayed at invoke.",
    )
    run_id: str | None = Field(
        default=None,
        description="Agent run this authorizes, when confirming for an agent call.",
    )


class ConfirmationResponse(BaseModel):
    """A one-time token authorizing the approved call."""

    confirmation_token: str = Field(
        description="Shown once; only its hash is stored. Present it at invoke.",
    )
    expires_at: str
    tool_name: str


@router.post("", response_model=ConfirmationResponse, status_code=201)
def create_confirmation(
    body: ConfirmationRequest,
    response: Response,
    session: CurrentSession,
    registry: RegistryDep,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ConfirmationResponse:
    """Issue a confirmation for one specific tool call.

    Refuses to confirm a tool that does not need one: handing out an
    authorization that nothing checks would train callers to request tokens
    reflexively, which is how a confirmation step becomes a rubber stamp.
    """
    response.headers["Cache-Control"] = "private, no-store"
    try:
        tool = registry.get(body.tool_name)
    except ToolNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "tool_not_found", "name": body.tool_name}
        ) from exc

    if not tool.requires_confirmation:
        raise HTTPException(
            status_code=409,
            detail={"code": "confirmation_not_required", "tool": tool.name},
        )

    now = datetime.now(UTC)
    expires_at = now + CONFIRMATION_TTL
    token = generate_confirmation_token()
    ConfirmationRepository(session_factory).issue(
        confirmation_id=str(uuid.uuid4()),
        token=token,
        binding=ConfirmationBinding(
            # Identity from the session: a user can only authorize their own call.
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            run_id=body.run_id,
            tool_name=tool.name,
            args_hash=canonical_args_hash(body.input),
            idempotency_key=body.idempotency_key,
        ),
        expires_at=expires_at,
        now=now,
    )
    return ConfirmationResponse(
        confirmation_token=token,
        expires_at=expires_at.isoformat(),
        tool_name=tool.name,
    )
