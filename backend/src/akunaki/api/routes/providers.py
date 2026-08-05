"""``GET /v1/providers`` — what can I link, and what would it give me?

Answers the question that comes *before* linking. ``/v1/connections`` describes
what the user already has; this describes what is on offer and what each option
actually contributes — including, crucially, whether a provider can produce a
recovery score on its own. Linking only Polar yields workouts and a permanently
``insufficient`` score, and nothing in the product says so today.

**Only configured providers are listed.** ``/v1/connections/{provider}/authorize``
deliberately returns an indistinguishable 404 for unknown and unconfigured
providers, so an unconfigured deployment cannot be probed for what it *could*
link. This surface must not be the back door around that: a provider without
complete OAuth credentials is absent here, exactly as it is there.

**Only code-enforced capabilities.** The design's capability matrix is labelled
*proposed targets*; publishing it would promise data that never arrives.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.oauth_client_factory import supported_link_providers
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.connections_surface import ConnectionsSurfaceService
from akunaki.config import Settings
from akunaki.domain.provider_capabilities import capabilities_for

router = APIRouter(prefix="/v1/providers", tags=["providers"])


class ProviderResponse(BaseModel):
    """One linkable provider, what it supplies, and whether it is connected."""

    provider: str
    capabilities: list[str] = Field(
        description=(
            "What this connector actually ingests: 'sleep', "
            "'overnight_vitals', 'workouts'. Implemented, not aspirational."
        ),
    )
    supports_recovery_score: bool = Field(
        description=(
            "Whether this provider alone can satisfy the recovery gate "
            "(sleep adherence plus HRV or resting HR). False means linking it "
            "on its own will never produce a score."
        ),
    )
    connection_status: str | None = Field(
        default=None,
        description=(
            "Status of the caller's connection to this provider: pending, "
            "active, needs_reauth, revoked, or error. Null when not linked."
        ),
    )


class ProvidersResponse(BaseModel):
    """The providers this deployment can link."""

    providers: list[ProviderResponse]


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


@router.get("", response_model=ProvidersResponse)
def list_providers(
    response: Response,
    session: CurrentSession,
    settings: Annotated[Settings, Depends(_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ProvidersResponse:
    """List the linkable providers with their capabilities and link status."""
    response.headers["Cache-Control"] = "private, no-store"

    summaries = ConnectionsSurfaceService(
        connections=ConnectionRepository(session_factory)
    ).connections_for_tenant(tenant_id=session.tenant_id)
    # At most one row per provider: ``uq_connections_tenant_provider`` enforces
    # it, and a revoke updates that row in place rather than adding another. So
    # a plain mapping cannot lose a live connection to a stale one.
    status_by_provider = {summary.provider: summary.status for summary in summaries}

    providers: list[ProviderResponse] = []
    for provider in sorted(supported_link_providers()):
        if settings.connector_oauth(provider) is None:
            # Unconfigured is invisible, matching the authorize route's 404.
            continue
        described = capabilities_for(provider)
        providers.append(
            ProviderResponse(
                provider=provider,
                capabilities=[capability.value for capability in described.capabilities],
                supports_recovery_score=described.supports_recovery_score,
                connection_status=status_by_provider.get(provider),
            )
        )

    return ProvidersResponse(providers=providers)
