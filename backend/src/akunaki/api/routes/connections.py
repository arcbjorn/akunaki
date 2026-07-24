"""Connector link routes: ``/v1/connections/{provider}/authorize`` + callback.

These wire the provider-uniform OAuth linking service into the two HTTP legs a
browser walks to link a wearable. Authenticated: the ``tenant_id`` comes from
the validated session, never a request parameter, so a caller can only link a
connection for their own tenant.

Only providers with fully-configured OAuth credentials are linkable; a request
for an unconfigured or unknown provider is a 404, so an unconfigured deployment
exposes no half-built connect surface.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.oauth_client_factory import (
    build_oauth_client,
    supported_link_providers,
)
from akunaki.adapters.crypto.config import build_sealer
from akunaki.adapters.crypto.oauth import (
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
)
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.oauth_linking import LinkRejection, OAuthLinkingService
from akunaki.config import ConnectorOAuthConfig, Settings

router = APIRouter(prefix="/v1/connections", tags=["connections"])


class AuthorizeResponse(BaseModel):
    """Where the client should redirect to authorize the connector."""

    authorize_url: str = Field(description="Provider authorize URL for the link flow.")
    provider: str


class LinkedResponse(BaseModel):
    """A completed connector link."""

    connection_id: str
    provider: str
    status: str


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _provider_config(provider: str, settings: Settings) -> ConnectorOAuthConfig:
    """Resolve a linkable provider's OAuth config, or 404.

    Unknown and unconfigured are the same 404: an unconfigured deployment must
    not reveal which providers *could* be linked if credentials were set.
    """
    if provider not in supported_link_providers():
        raise HTTPException(status_code=404, detail={"code": "unknown_provider"})
    config = settings.connector_oauth(provider)
    if config is None:
        raise HTTPException(status_code=404, detail={"code": "provider_not_configured"})
    return config


def _linking_service(
    provider: str,
    settings: Settings,
    session_factory: sessionmaker[Session],
    config: ConnectorOAuthConfig,
) -> OAuthLinkingService:
    client = build_oauth_client(provider, config)
    return OAuthLinkingService(
        client=client,
        states=OAuthStateRepository(session_factory),
        connections=ConnectionRepository(session_factory),
        sealer=build_sealer(settings),
        generate_state=generate_state,
        generate_code_verifier=generate_code_verifier,
        code_challenge=code_challenge_s256,
        new_id=lambda: str(uuid.uuid4()),
    )


@router.get("/{provider}/authorize", response_model=AuthorizeResponse)
def authorize(
    provider: str,
    response: Response,
    session: CurrentSession,
    settings: Annotated[Settings, Depends(_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> AuthorizeResponse:
    """Begin a connector link for the caller's tenant; return the authorize URL."""
    response.headers["Cache-Control"] = "private, no-store"
    config = _provider_config(provider, settings)
    service = _linking_service(provider, settings, session_factory, config)
    redirect = service.start_link(
        tenant_id=session.tenant_id,
        redirect_uri=config.redirect_uri,
        scopes=_DEFAULT_SCOPES[provider],
        now=datetime.now(UTC),
    )
    return AuthorizeResponse(authorize_url=redirect.authorize_url, provider=provider)


@router.get("/{provider}/callback", response_model=LinkedResponse)
def callback(
    provider: str,
    response: Response,
    session: CurrentSession,
    settings: Annotated[Settings, Depends(_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    state: Annotated[str, Query(min_length=1)],
    code: Annotated[str, Query(min_length=1)],
) -> LinkedResponse:
    """Complete a connector callback: exchange the code, store sealed tokens."""
    response.headers["Cache-Control"] = "private, no-store"
    config = _provider_config(provider, settings)
    service = _linking_service(provider, settings, session_factory, config)
    result = service.complete_link(
        state=state,
        code=code,
        redirect_uri=config.redirect_uri,
        now=datetime.now(UTC),
    )
    if not result.ok or result.connection is None:
        raise HTTPException(
            status_code=_status_for(result.rejection), detail={"code": "link_failed"}
        )
    # The state's tenant is the authoritative one; a session for a different
    # tenant must not claim someone else's in-flight authorization.
    if result.connection.tenant_id != session.tenant_id:
        raise HTTPException(status_code=404, detail={"code": "link_failed"})
    return LinkedResponse(
        connection_id=result.connection.connection_id,
        provider=provider,
        status=result.connection.status.value,
    )


def _status_for(rejection: LinkRejection | None) -> int:
    """A transient provider failure is a 503; everything else a 400."""
    if rejection is not None and rejection.retryable:
        return 503
    return 400


# Default scopes requested per provider at authorize time.
_DEFAULT_SCOPES: dict[str, tuple[str, ...]] = {
    "oura": ("daily",),
    "polar": ("accesslink.read_all",),
    "google_health": ("https://www.googleapis.com/auth/health.sleep.read",),
}


__all__ = ["router"]
