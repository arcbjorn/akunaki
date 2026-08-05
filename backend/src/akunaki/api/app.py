"""Application factory for the core API process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import (
    create_db_engine,
    create_session_factory,
    probe_database_ready,
)
from akunaki.api.headers import security_headers_middleware
from akunaki.api.request_context import request_id_middleware
from akunaki.api.routes.health import router as health_router
from akunaki.config import Settings, get_settings

# Only these headers may accompany a credentialed cross-origin request.
_CORS_ALLOWED_HEADERS = ("content-type", "x-akunaki-csrf", "x-request-id")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app with core wiring only (no model config or SDKs)."""
    resolved = settings if settings is not None else get_settings()
    engine = create_db_engine(resolved)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.engine = engine
        app.state.session_factory = session_factory
        yield
        engine.dispose()

    app = FastAPI(
        title="Akunaki 飽くなき API",
        version="0.1.0",
        description=(
            "Core platform API foundation. Product surfaces and model/agent "
            "paths are not present in this build."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.probe_database_ready = lambda: probe_database_ready(engine)

    # Security headers on every response, including errors.
    app.middleware("http")(security_headers_middleware)

    # Request-id binding runs **outermost** (registered last, so it wraps the
    # others): the correlation id is bound before any downstream middleware or
    # handler logs, and echoed on the response.
    app.middleware("http")(request_id_middleware)

    # CORS only for the configured browser origins, with credentials. An empty
    # allow-list means no cross-origin browser access (same-origin deployment);
    # a credentialed request never uses a wildcard origin.
    if resolved.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=list(_CORS_ALLOWED_HEADERS),
            # Let a browser client read the correlation id off the response.
            expose_headers=["x-request-id"],
        )

    app.include_router(health_router)

    # Readiness is imported lazily: it imports the app module for the engine /
    # session-factory dependencies, so a top-level import would cycle.
    from akunaki.api.routes.ready import router as ready_router

    app.include_router(ready_router)

    # Session routes are always mounted: every endpoint on them requires a
    # valid session cookie, so mounting them exposes nothing on its own.
    from akunaki.api.routes.anomalies import router as anomalies_router
    from akunaki.api.routes.checkin import router as checkin_router
    from akunaki.api.routes.confirmations import router as confirmations_router
    from akunaki.api.routes.connections import router as connections_router
    from akunaki.api.routes.data_quality import router as data_quality_router
    from akunaki.api.routes.me import router as me_router
    from akunaki.api.routes.metrics_series import router as metrics_series_router
    from akunaki.api.routes.privacy import router as privacy_router
    from akunaki.api.routes.provenance import router as provenance_router
    from akunaki.api.routes.providers import router as providers_router
    from akunaki.api.routes.recommendations import router as recommendations_router
    from akunaki.api.routes.recovery import router as recovery_router
    from akunaki.api.routes.session import router as session_router
    from akunaki.api.routes.sleep import router as sleep_router
    from akunaki.api.routes.source_policies import router as source_policies_router
    from akunaki.api.routes.sync_status import router as sync_status_router
    from akunaki.api.routes.today import router as today_router
    from akunaki.api.routes.tools import router as tools_router
    from akunaki.api.routes.trends import router as trends_router
    from akunaki.api.routes.webhooks import router as webhooks_router
    from akunaki.api.routes.workouts import router as workouts_router

    app.include_router(session_router)
    app.include_router(sleep_router)
    app.include_router(recovery_router)
    app.include_router(today_router)
    app.include_router(checkin_router)
    app.include_router(tools_router)
    app.include_router(provenance_router)
    app.include_router(connections_router)
    app.include_router(privacy_router)
    app.include_router(anomalies_router)
    app.include_router(workouts_router)
    app.include_router(confirmations_router)
    app.include_router(source_policies_router)
    app.include_router(metrics_series_router)
    app.include_router(trends_router)
    app.include_router(recommendations_router)
    app.include_router(providers_router)
    app.include_router(me_router)
    app.include_router(sync_status_router)
    app.include_router(data_quality_router)
    app.include_router(webhooks_router)

    # Login routes only when OIDC is configured. An unconfigured deployment
    # exposes no half-built auth surface.
    if resolved.oidc_issuer.strip():
        from akunaki.api.routes.auth import router as auth_router

        app.include_router(auth_router)

    # Metrics only when enabled: the endpoint is unauthenticated (a scraper
    # cannot hold a session cookie), so it is not registered at all unless the
    # deployment opts in and can restrict who reaches the port.
    if resolved.metrics_enabled:
        from akunaki.api.routes.metrics import router as metrics_router

        app.include_router(metrics_router)

    return app


def get_engine(request: Request) -> Engine:
    """Resolve the process engine from app state."""
    return request.app.state.engine  # type: ignore[no-any-return]


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Resolve the session factory from app state."""
    return request.app.state.session_factory  # type: ignore[no-any-return]
