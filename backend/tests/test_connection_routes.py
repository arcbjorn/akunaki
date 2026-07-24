"""End-to-end coverage of the connector link routes over real HTTP.

Verify the authenticated authorize/callback legs link a connection for the
caller's tenant, that an unconfigured or unknown provider is a 404 (no
half-built connect surface), and that the routes require a session. Polar is the
configured provider; its token endpoint is served in-process by patching the
route's ``build_oauth_client`` to return a Polar client over a mock transport.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import akunaki.api.routes.connections as connections_mod
from akunaki.adapters.connectors.polar import PolarOAuthClient
from akunaki.adapters.db.models import Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import ConnectorOAuthConfig, Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK_B64 = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="  # 32 bytes, base64
REDIRECT = "https://app.example.com/oauth/polar/callback"

POLAR_TOKEN_BODY = {
    "access_token": "polar-access-SECRET",
    "token_type": "bearer",
    "expires_in": 86400,
    "x_user_id": 555,
}


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        secret_keks=f"v1:{KEK_B64}",
        active_kek_version="v1",
        polar_client_id="pid",
        polar_client_secret="psecret",
        polar_redirect_uri=REDIRECT,
    )


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "conn_routes.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    command.upgrade(cfg, "head")
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(route_db: str) -> Iterator[sessionmaker[Session]]:
    from akunaki.adapters.db.engine import create_db_engine, create_session_factory

    engine = create_db_engine(_settings(route_db))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
        session.add(
            User(
                id="user-1",
                tenant_id="tenant-1",
                oidc_issuer="https://idp.example.com",
                oidc_subject="subject-1",
                email=None,
                created_at=NOW_S,
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def client(route_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Patch the client factory so Polar's token endpoint is served in-process.
    def _mock_client(provider: str, config: ConnectorOAuthConfig) -> PolarOAuthClient:
        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=POLAR_TOKEN_BODY)

        return PolarOAuthClient(
            client_id=config.client_id,
            client_secret=config.client_secret,
            transport=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setattr(connections_mod, "build_oauth_client", _mock_client)
    yield TestClient(create_app(_settings(route_db)))


def _login(client: TestClient, factory: sessionmaker[Session]) -> None:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def test_authorize_returns_a_provider_url(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)
    resp = client.get("/v1/connections/polar/authorize")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "polar"
    parsed = urlparse(body["authorize_url"])
    assert parsed.netloc == "flow.polar.com"
    assert "state" in parse_qs(parsed.query)


def test_full_link_flow_over_http(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    # Authorize, capture the state, then complete the callback.
    authorize = client.get("/v1/connections/polar/authorize").json()
    state = parse_qs(urlparse(authorize["authorize_url"]).query)["state"][0]

    resp = client.get(
        "/v1/connections/polar/callback",
        params={"state": state, "code": "auth-code-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "polar"
    assert body["status"] == "active"
    assert body["connection_id"]


def test_unknown_provider_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    resp = client.get("/v1/connections/garmin/authorize")
    assert resp.status_code == 404


def test_unconfigured_provider_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    # Oura is a supported provider but has no credentials configured here.
    _login(client, factory)
    resp = client.get("/v1/connections/oura/authorize")
    assert resp.status_code == 404


def test_authorize_requires_a_session(client: TestClient) -> None:
    client.cookies.clear()
    resp = client.get("/v1/connections/polar/authorize")
    assert resp.status_code == 401
