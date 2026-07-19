"""OIDC login routes: ``/auth/login`` and ``/auth/callback``.

These are the last piece: they wire the OIDC client, login state, user
provisioning, and session issuance into the two HTTP legs a browser walks
through. Mounted only when OIDC is configured, so an unconfigured deployment
exposes no half-built login surface.

The callback sets the session cookie on success and returns the CSRF secret in
the body — the cookie authenticates, the CSRF secret is what a SPA echoes on
later mutations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.config import build_sealer
from akunaki.adapters.crypto.oauth import (
    code_challenge_s256,
    generate_code_verifier,
    generate_nonce,
    generate_state,
)
from akunaki.adapters.db.login_state_repository import LoginStateRepository
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.adapters.db.user_repository import UserRepository
from akunaki.adapters.oidc.client import OIDCClient
from akunaki.api.app import get_session_factory
from akunaki.api.security import CSRF_HEADER_NAME, set_session_cookie
from akunaki.application.login import LoginService
from akunaki.config import Settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginStartResponse(BaseModel):
    """Where the client should redirect to authenticate."""

    authorize_url: str = Field(description="Provider authorize URL for a PKCE login.")


class LoginCompleteResponse(BaseModel):
    """A completed login. The session cookie is set on the response."""

    tenant_id: str
    user_id: str
    csrf_secret: str = Field(
        description="Echo this in the X-Akunaki-CSRF header on later mutations."
    )
    session_expires_at: str


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _login_service(
    request: Request,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> LoginService:
    settings: Settings = request.app.state.settings
    client = OIDCClient(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
    )
    return LoginService(
        client=client,
        states=LoginStateRepository(session_factory),
        users=UserRepository(session_factory),
        sessions=SessionRepository(session_factory),
        sealer=build_sealer(settings),
        generate_state=generate_state,
        generate_nonce=generate_nonce,
        generate_code_verifier=generate_code_verifier,
        code_challenge=code_challenge_s256,
        new_id=lambda: str(uuid.uuid4()),
    )


LoginServiceDep = Annotated[LoginService, Depends(_login_service)]


@router.get("/login", response_model=LoginStartResponse)
def login_start(
    response: Response,
    service: LoginServiceDep,
    settings: Annotated[Settings, Depends(_settings)],
) -> LoginStartResponse:
    """Begin an OIDC login and return the authorize URL."""
    response.headers["Cache-Control"] = "private, no-store"
    redirect = service.begin(
        redirect_uri=settings.oidc_redirect_uri,
        now=datetime.now(UTC),
    )
    return LoginStartResponse(authorize_url=redirect.authorize_url)


@router.get("/callback", response_model=LoginCompleteResponse)
def login_callback(
    response: Response,
    service: LoginServiceDep,
    settings: Annotated[Settings, Depends(_settings)],
    state: Annotated[str, Query(min_length=1)],
    code: Annotated[str, Query(min_length=1)],
) -> LoginCompleteResponse:
    """Complete a callback: verify the token and issue a session."""
    response.headers["Cache-Control"] = "private, no-store"
    result = service.complete(
        state=state,
        code=code,
        redirect_uri=settings.oidc_redirect_uri,
        now=datetime.now(UTC),
    )
    if not result.ok:
        # One generic error: which check failed is server-side metrics only.
        raise HTTPException(status_code=401, detail={"code": "unauthenticated"})

    assert result.session_token is not None
    max_age = _cookie_max_age(result.session_expires_at)
    set_session_cookie(
        response,
        token=result.session_token,
        max_age_seconds=max_age,
        secure=settings.session_cookie_secure,
    )
    return LoginCompleteResponse(
        tenant_id=result.tenant_id or "",
        user_id=result.user_id or "",
        csrf_secret=result.csrf_secret or "",
        session_expires_at=result.session_expires_at or "",
    )


def _cookie_max_age(expires_at: str | None) -> int:
    """Seconds until the session expires, floored at zero."""
    if not expires_at:
        return 0
    from akunaki.domain.jobs import parse_utc_rfc3339

    delta = parse_utc_rfc3339(expires_at) - datetime.now(UTC)
    return max(int(delta.total_seconds()), 0)


# The CSRF header name is exported so a client SDK can reference one constant.
__all__ = ["CSRF_HEADER_NAME", "router"]
