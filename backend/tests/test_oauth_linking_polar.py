"""OAuth linking service for a non-PKCE provider (Polar).

The linking service now supports providers that do not use PKCE. This wires the
real state repository, sealer, connection repository, and **PolarOAuthClient**
(Basic-auth token exchange, no PKCE) over a mock transport, proving the full
authorize -> callback flow links a Polar connection: the authorize URL carries
no code challenge, the state's sealed placeholder still gates the exchange, and
Polar's ``x_user_id`` lands as the connection's ``external_user_id``.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.polar import PolarOAuthClient
from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.crypto.oauth import (
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
)
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository
from akunaki.application.oauth_linking import LinkRejection, OAuthLinkingService
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import ConnectionStatus, Provider
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
REDIRECT = "https://app.example.com/oauth/polar/callback"
KEK = b"\x55" * KEY_BYTES
ACCESS_TOKEN = "polar-access-SECRET"

TOKEN_BODY = {
    "access_token": ACCESS_TOKEN,
    "token_type": "bearer",
    "expires_in": 86400,
    "x_user_id": 987654,
}


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def link_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "link_polar.db"
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
def factory(link_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=link_db))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at=to_utc_rfc3339(T0),
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _service(
    factory: sessionmaker[Session],
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> OAuthLinkingService:
    ids = (f"id-{n}" for n in itertools.count(1))
    client = PolarOAuthClient(
        client_id="cid",
        client_secret="csecret",
        transport=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return OAuthLinkingService(
        client=client,
        states=OAuthStateRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        generate_state=generate_state,
        generate_code_verifier=generate_code_verifier,
        code_challenge=code_challenge_s256,
        new_id=lambda: next(ids),
    )


def _token_ok(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=TOKEN_BODY)


def _state_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def test_authorize_url_carries_no_pkce_challenge(factory: sessionmaker[Session]) -> None:
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1",
        redirect_uri=REDIRECT,
        scopes=("accesslink.read_all",),
        now=T0,
    )
    params = parse_qs(urlparse(redirect.authorize_url).query)
    assert "state" in params
    # Non-PKCE: no challenge is added to the authorize URL.
    assert "code_challenge" not in params
    assert "code_challenge_method" not in params


def test_full_non_pkce_flow_links_connection(factory: sessionmaker[Session], link_db: str) -> None:
    service = _service(factory, _token_ok)

    redirect = service.start_link(
        tenant_id="tenant-1",
        redirect_uri=REDIRECT,
        scopes=("accesslink.read_all",),
        now=T0,
    )
    state = _state_from(redirect.authorize_url)

    result = service.complete_link(
        state=state,
        code="auth-code-1",
        redirect_uri=REDIRECT,
        now=T0 + timedelta(minutes=1),
    )

    assert result.ok
    assert result.connection is not None
    assert result.connection.provider is Provider.POLAR
    assert result.connection.status is ConnectionStatus.ACTIVE
    # Polar's x_user_id lands as the connection's external user id.
    assert result.connection.external_user_id == "987654"

    # Tokens are stored only as ciphertext.
    engine = create_db_engine(Settings(database_url=link_db))
    try:
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT ciphertext FROM connection_secrets")).scalar_one()
    finally:
        engine.dispose()
    assert ACCESS_TOKEN.encode() not in stored


def test_replayed_callback_is_rejected(factory: sessionmaker[Session]) -> None:
    # Single-use protection is identical for a non-PKCE flow: the state row is
    # consumed once even though its sealed verifier is an empty placeholder.
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("accesslink.read_all",), now=T0
    )
    state = _state_from(redirect.authorize_url)

    first = service.complete_link(state=state, code="c", redirect_uri=REDIRECT, now=T0)
    second = service.complete_link(state=state, code="c", redirect_uri=REDIRECT, now=T0)
    assert first.ok
    assert second.rejection is LinkRejection.INVALID_STATE
