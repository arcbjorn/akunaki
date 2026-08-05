"""``GET /v1/me`` — the caller's own account and tenant.

Distinct from ``/v1/session``, which answers "is this cookie valid and whose is
it": session id, user id, tenant id, expiry. Nothing about the *account*. So a
client can hold a valid session and still not know the user's email, when they
joined, or — the consequential one — **which timezone their days are in**.

``primary_timezone`` is stored on every tenant at signup and, until now, read by
nothing. Every day surface (``/v1/today``, ``/v1/recovery``, ``/v1/metrics``,
``/v1/recommendations``) requires an explicit ``day`` precisely because the
server must never guess a local health day from its own clock — but the client
had no way to learn which timezone defines that day either. This closes that
loop.

It is the tenant's **stated preference**, not an engine input: a fact's local
health day is derived from that payload's own offset at normalization time.
A client should use it to choose which ``day`` to request, not to reinterpret
days already returned.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.user_repository import UserRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession

router = APIRouter(prefix="/v1/me", tags=["me"])


class TenantBlock(BaseModel):
    """The caller's tenant. MVP tenancy is one user per tenant."""

    tenant_id: str
    status: str = Field(description="active, suspended, or pending_delete.")
    primary_timezone: str = Field(
        description=(
            "IANA timezone the tenant's days are stated in (default 'UTC'). "
            "Use it to choose which 'day' to request from the day surfaces; it "
            "does not reinterpret days already returned."
        ),
    )
    display_name: str | None = Field(default=None, description="Sensitive PII; null until set.")


class MeResponse(BaseModel):
    """The caller's own account.

    Carries no session or credential material: ``/v1/session`` owns session
    state, and the OIDC subject is never echoed.
    """

    user_id: str
    email: str | None = Field(
        default=None,
        description="Sensitive PII; null when the IdP released no email.",
    )
    created_at: str = Field(description="UTC RFC3339.")
    tenant: TenantBlock


def _users(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> UserRepository:
    return UserRepository(session_factory)


@router.get("", response_model=MeResponse)
def read_me(
    response: Response,
    session: CurrentSession,
    users: Annotated[UserRepository, Depends(_users)],
) -> MeResponse:
    """Return the caller's own account and tenant."""
    response.headers["Cache-Control"] = "private, no-store"
    account = users.account_for(user_id=session.user_id, tenant_id=session.tenant_id)
    if account is None:
        # Not reachable from a validated session: ``sessions`` cascades from
        # both ``users`` and ``tenants``, and the privacy scrub deletes the
        # tenant row, so a session cannot outlive its account. Handled anyway
        # because the alternative is an AttributeError if that ever changes —
        # a 404 is the honest answer for an account that does not exist.
        raise HTTPException(status_code=404, detail={"code": "account_not_found"})

    return MeResponse(
        user_id=account.user_id,
        email=account.email,
        created_at=account.created_at,
        tenant=TenantBlock(
            tenant_id=account.tenant_id,
            status=account.tenant_status,
            primary_timezone=account.primary_timezone,
            display_name=account.display_name,
        ),
    )
