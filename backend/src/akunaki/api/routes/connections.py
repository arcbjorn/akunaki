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
from akunaki.application.connections_surface import ConnectionsSurfaceService
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


class ConnectionStatusResponse(BaseModel):
    """One linked connection's status and ingest progress."""

    connection_id: str
    provider: str
    status: str = Field(description="pending, active, needs_reauth, revoked, or error.")
    last_success_at: str | None = Field(description="Last successful sync, UTC RFC3339.")
    last_error_class: str | None = Field(
        description="Error class only; never a vendor body or message.",
    )
    consecutive_failures: int
    transport_pages: int = Field(description="Raw vendor responses retained.")
    raw_revisions: int = Field(description="Logical records ingested for this connection.")


class ConnectionsResponse(BaseModel):
    """The caller's connections. Empty when nothing is linked."""

    connections: list[ConnectionStatusResponse]


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


@router.get("", response_model=ConnectionsResponse)
def list_connections(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ConnectionsResponse:
    """List the caller's connections with their sync status.

    Tenant-scoped from the session: a caller sees only their own connections,
    and cannot ask for another tenant's by parameter. Carries no health values
    — statuses, timestamps, and ingest counts only.
    """
    response.headers["Cache-Control"] = "private, no-store"
    service = ConnectionsSurfaceService(connections=ConnectionRepository(session_factory))
    return ConnectionsResponse(
        connections=[
            ConnectionStatusResponse(
                connection_id=summary.connection_id,
                provider=summary.provider,
                status=summary.status,
                last_success_at=summary.last_success_at,
                last_error_class=summary.last_error_class,
                consecutive_failures=summary.consecutive_failures,
                transport_pages=summary.transport_pages,
                raw_revisions=summary.raw_revisions,
            )
            for summary in service.connections_for_tenant(tenant_id=session.tenant_id)
        ]
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
        scopes=DEFAULT_SCOPES[provider],
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


# Default scopes requested per provider at authorize time. Least privilege: each
# provider is asked only for the streams its connector actually fetches
# (see `sync_config_for_provider`), never a blanket read-everything grant.
DEFAULT_SCOPES: dict[str, tuple[str, ...]] = {
    # Oura backfills the detailed `sleep` collection (which also carries the
    # overnight vitals). Verified 2026-07-25 against the official OpenAPI spec
    # (api.ouraring.com/v2/static/json/openapi-1.37.json): every endpoint
    # declares `OAuth2: []` — an **empty** scope list — so Oura publishes no
    # per-endpoint scope mapping at all. Of the eight real scopes, `daily`
    # ("daily summaries of sleep, activity and readiness") is the only one
    # mentioning sleep, so it is the correct ask; the others (email, personal,
    # heartrate, workout, tag, session, spo2Daily) cover data no connector
    # reads and are omitted for least privilege.
    #
    # Caveat worth knowing: an under-scoped Oura token returns an **empty
    # array, not an error**, so a scope shortfall would look like "no data"
    # rather than failing loudly. `InitialSyncHandler` therefore warns when a
    # full-lookback backfill yields zero records.
    "oura": ("daily",),
    "polar": ("accesslink.read_all",),
    # Google Health scopes are all `.../auth/googlehealth.*` and are Restricted
    # (they require Google's security review before production use).
    "google_health": ("https://www.googleapis.com/auth/googlehealth.sleep.readonly",),
}


__all__ = ["DEFAULT_SCOPES", "router"]
