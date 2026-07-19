"""Application factory for the core API process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import (
    create_db_engine,
    create_session_factory,
    probe_database_ready,
)
from akunaki.api.routes.health import router as health_router
from akunaki.config import Settings, get_settings


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
        title="Akunaki API",
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

    app.include_router(health_router)

    # Session routes are always mounted: every endpoint on them requires a
    # valid session cookie, so mounting them exposes nothing on its own.
    from akunaki.api.routes.session import router as session_router
    from akunaki.api.routes.sleep import router as sleep_router

    app.include_router(session_router)
    app.include_router(sleep_router)

    # Login routes only when OIDC is configured. An unconfigured deployment
    # exposes no half-built auth surface.
    if resolved.oidc_issuer.strip():
        from akunaki.api.routes.auth import router as auth_router

        app.include_router(auth_router)

    if resolved.debug_routes_enabled:
        # Imported lazily so the unauthenticated router cannot be reached at
        # all — not even as a registered-but-guarded path — unless explicitly
        # enabled. It serves tenant health data with no session check.
        from akunaki.api.routes.debug import router as debug_router

        app.include_router(debug_router)

    return app


def get_engine(request: Request) -> Engine:
    """Resolve the process engine from app state."""
    return request.app.state.engine  # type: ignore[no-any-return]


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Resolve the session factory from app state."""
    return request.app.state.session_factory  # type: ignore[no-any-return]
