"""Session-scoped routes: who am I, and log out.

These demonstrate the authenticated pattern the ``/v1`` product surface will
use: the tenant comes from the validated session, never from a client-supplied
parameter, so a caller cannot read another tenant's data by asking nicely.

Login is deliberately absent — issuing a session requires the OIDC handshake,
which is blocked on the final IdP choice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession, clear_session_cookie

router = APIRouter(prefix="/v1/session", tags=["session"])


class CurrentSessionResponse(BaseModel):
    """The caller's own session. Carries no secret material."""

    session_id: str
    user_id: str
    tenant_id: str
    expires_at: str = Field(description="UTC RFC3339.")


class LogoutResponse(BaseModel):
    """Result of a logout."""

    revoked: bool = Field(description="False when the session was already revoked.")


class LogoutEverywhereResponse(BaseModel):
    """Result of a logout-everywhere."""

    revoked_count: int = Field(description="How many live sessions were revoked.")


def _sessions(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> SessionRepository:
    return SessionRepository(session_factory)


@router.get("", response_model=CurrentSessionResponse)
def current_session(response: Response, session: CurrentSession) -> CurrentSessionResponse:
    """Return the caller's own session."""
    response.headers["Cache-Control"] = "private, no-store"
    return CurrentSessionResponse(
        session_id=session.session_id,
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        expires_at=session.expires_at,
    )


@router.post("/logout", response_model=LogoutResponse)
def logout(
    response: Response,
    session: CurrentSession,
    sessions: Annotated[SessionRepository, Depends(_sessions)],
) -> LogoutResponse:
    """Revoke the session server-side and clear the cookie.

    Both halves are required: clearing the cookie alone would leave a valid
    session usable by anyone who captured the token.
    """
    revoked = sessions.revoke(session_id=session.session_id, now=datetime.now(UTC))
    clear_session_cookie(response)
    response.headers["Cache-Control"] = "private, no-store"
    return LogoutResponse(revoked=revoked)


@router.post("/logout-everywhere", response_model=LogoutEverywhereResponse)
def logout_everywhere(
    response: Response,
    session: CurrentSession,
    sessions: Annotated[SessionRepository, Depends(_sessions)],
) -> LogoutEverywhereResponse:
    """Revoke **every** live session for the user, and clear this cookie.

    For a stolen-device or password-change scenario: one call ends every
    session the user holds, not just this browser's. The current cookie is
    cleared too, so the caller is logged out here as well.
    """
    count = sessions.revoke_all_for_user(user_id=session.user_id, now=datetime.now(UTC))
    clear_session_cookie(response)
    response.headers["Cache-Control"] = "private, no-store"
    return LogoutEverywhereResponse(revoked_count=count)
