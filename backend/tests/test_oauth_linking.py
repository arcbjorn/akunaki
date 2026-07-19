"""OAuth linking service: full authorize -> callback flow.

Wired against the **real** state repository, sealer, connection repository, and
Oura client (over a mock transport), so this exercises the actual security
rules rather than a stack of doubles.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.oura import OuraOAuthClient
from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.crypto.oauth import (
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
)
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Connection, ConnectionSecret, OAuthState, Tenant
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository
from akunaki.application.oauth_linking import LinkRejection, OAuthLinkingService
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import ConnectionStatus, Provider
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
REDIRECT = "https://app.example.com/oauth/oura/callback"
KEK = b"\x44" * KEY_BYTES
ACCESS_TOKEN = "oura-access-SECRET"
REFRESH_TOKEN = "oura-refresh-SECRET"

TOKEN_BODY = {
    "access_token": ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
    "expires_in": 86400,
    "scope": "daily personal",
    "token_type": "Bearer",
}


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def link_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "link.db"
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
    """Build the service from real components; only the network is faked."""
    ids = (f"id-{n}" for n in itertools.count(1))
    client = OuraOAuthClient(
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
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["state"][0]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_full_link_flow_persists_sealed_tokens(
    factory: sessionmaker[Session], link_db: str
) -> None:
    service = _service(factory, _token_ok)

    redirect = service.start_link(
        tenant_id="tenant-1",
        redirect_uri=REDIRECT,
        scopes=("daily", "personal"),
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
    assert result.connection.tenant_id == "tenant-1"
    assert result.connection.provider is Provider.OURA
    assert result.connection.status is ConnectionStatus.ACTIVE
    assert result.connection.scopes == ("daily", "personal")

    # Tokens are stored only as ciphertext.
    engine = create_db_engine(Settings(database_url=link_db))
    try:
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT ciphertext FROM connection_secrets")).scalar_one()
    finally:
        engine.dispose()
    assert ACCESS_TOKEN.encode() not in stored
    assert REFRESH_TOKEN.encode() not in stored

    # ...and they open again under the connection's identity.
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    sealed = ConnectionRepository(factory).get_sealed_secret(
        connection_id=result.connection.connection_id
    )
    assert sealed is not None
    opened = json.loads(sealer.open(sealed, aad=result.connection.connection_id.encode()))
    assert opened["access_token"] == ACCESS_TOKEN
    assert opened["refresh_token"] == REFRESH_TOKEN


def test_authorize_url_uses_s256_challenge_for_the_stored_verifier(
    factory: sessionmaker[Session],
) -> None:
    """The challenge on the URL must match the verifier that was sealed."""
    from urllib.parse import parse_qs, urlparse

    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )

    params = parse_qs(urlparse(redirect.authorize_url).query)
    assert params["code_challenge_method"][0] == "S256"

    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    with factory() as session:
        row = session.get(OAuthState, redirect.state_id)
        assert row is not None
        verifier = sealer.open(_sealed_of(row), aad=redirect.state_id.encode()).decode()
    assert code_challenge_s256(verifier) == params["code_challenge"][0]


def _sealed_of(row: OAuthState):  # type: ignore[no-untyped-def]
    from akunaki.domain.secrets import SealedSecret

    return SealedSecret(
        ciphertext=row.code_verifier_ciphertext,
        key_version=row.code_verifier_key_version,
    )


# ---------------------------------------------------------------------------
# Callback rejections
# ---------------------------------------------------------------------------


def test_replayed_callback_is_rejected(factory: sessionmaker[Session]) -> None:
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    state = _state_from(redirect.authorize_url)

    first = service.complete_link(state=state, code="c", redirect_uri=REDIRECT, now=T0)
    second = service.complete_link(state=state, code="c", redirect_uri=REDIRECT, now=T0)

    assert first.ok
    assert second.rejection is LinkRejection.INVALID_STATE


def test_forged_state_is_rejected(factory: sessionmaker[Session]) -> None:
    service = _service(factory, _token_ok)
    service.start_link(tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0)

    result = service.complete_link(state=generate_state(), code="c", redirect_uri=REDIRECT, now=T0)
    assert result.rejection is LinkRejection.INVALID_STATE


def test_mismatched_redirect_is_rejected(factory: sessionmaker[Session]) -> None:
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    state = _state_from(redirect.authorize_url)

    result = service.complete_link(
        state=state, code="c", redirect_uri="https://evil.test/cb", now=T0
    )
    assert result.rejection is LinkRejection.INVALID_STATE


def test_expired_state_is_rejected(factory: sessionmaker[Session]) -> None:
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    state = _state_from(redirect.authorize_url)

    result = service.complete_link(
        state=state, code="c", redirect_uri=REDIRECT, now=T0 + timedelta(hours=1)
    )
    assert result.rejection is LinkRejection.INVALID_STATE


def test_missing_code_does_not_consume_the_state(factory: sessionmaker[Session]) -> None:
    """A provider-denied callback must not burn the state."""
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    state = _state_from(redirect.authorize_url)

    denied = service.complete_link(state=state, code="", redirect_uri=REDIRECT, now=T0)
    retried = service.complete_link(state=state, code="c", redirect_uri=REDIRECT, now=T0)

    assert denied.rejection is LinkRejection.INVALID_STATE
    assert retried.ok


def test_no_connection_is_created_when_state_is_invalid(
    factory: sessionmaker[Session],
) -> None:
    service = _service(factory, _token_ok)
    service.start_link(tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0)

    service.complete_link(state=generate_state(), code="c", redirect_uri=REDIRECT, now=T0)

    with factory() as session:
        assert session.scalars(select(Connection)).all() == []
        assert session.scalars(select(ConnectionSecret)).all() == []


# ---------------------------------------------------------------------------
# Provider failures
# ---------------------------------------------------------------------------


def test_invalid_grant_is_not_retryable(factory: sessionmaker[Session]) -> None:
    def rejected(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": "invalid_grant"})

    service = _service(factory, rejected)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )

    result = service.complete_link(
        state=_state_from(redirect.authorize_url), code="c", redirect_uri=REDIRECT, now=T0
    )

    assert result.rejection is LinkRejection.PROVIDER_REJECTED
    # Must drive re-authorization, never a retry loop.
    assert result.rejection.retryable is False


def test_provider_outage_is_retryable(factory: sessionmaker[Session]) -> None:
    def unavailable(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"error": "temporarily_unavailable"})

    service = _service(factory, unavailable)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )

    result = service.complete_link(
        state=_state_from(redirect.authorize_url), code="c", redirect_uri=REDIRECT, now=T0
    )

    assert result.rejection is LinkRejection.PROVIDER_UNAVAILABLE
    assert result.rejection.retryable is True


def test_failed_exchange_leaves_no_half_written_connection(
    factory: sessionmaker[Session],
) -> None:
    """No connection row may exist without usable token material."""

    def rejected(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": "invalid_grant"})

    service = _service(factory, rejected)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    service.complete_link(
        state=_state_from(redirect.authorize_url), code="c", redirect_uri=REDIRECT, now=T0
    )

    with factory() as session:
        assert session.scalars(select(Connection)).all() == []
        assert session.scalars(select(ConnectionSecret)).all() == []


def test_unreadable_verifier_is_reported_distinctly(
    factory: sessionmaker[Session],
) -> None:
    """A KEK gap must not be reported as a provider or state failure."""
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    state = _state_from(redirect.authorize_url)

    # Corrupt the stored envelope so it can no longer be opened.
    with factory() as session, session.begin():
        row = session.get(OAuthState, redirect.state_id)
        assert row is not None
        row.code_verifier_ciphertext = row.code_verifier_ciphertext[:-4] + b"\x00\x00\x00\x00"

    result = service.complete_link(state=state, code="c", redirect_uri=REDIRECT, now=T0)

    assert result.rejection is LinkRejection.VERIFIER_UNREADABLE


# ---------------------------------------------------------------------------
# Relink and status
# ---------------------------------------------------------------------------


def test_relinking_reuses_the_connection_row(factory: sessionmaker[Session]) -> None:
    """Re-consent must not create a second connection for the same provider."""
    service = _service(factory, _token_ok)

    first_redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    first = service.complete_link(
        state=_state_from(first_redirect.authorize_url),
        code="c1",
        redirect_uri=REDIRECT,
        now=T0,
    )

    later = T0 + timedelta(days=30)
    second_redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=later
    )
    second = service.complete_link(
        state=_state_from(second_redirect.authorize_url),
        code="c2",
        redirect_uri=REDIRECT,
        now=later,
    )

    assert first.ok and second.ok
    assert second.connection is not None and first.connection is not None
    # Same row, so foreign keys elsewhere stay valid.
    assert second.connection.connection_id == first.connection.connection_id
    with factory() as session:
        assert len(session.scalars(select(Connection)).all()) == 1


def test_mark_needs_reauth_transitions_status(factory: sessionmaker[Session]) -> None:
    service = _service(factory, _token_ok)
    redirect = service.start_link(
        tenant_id="tenant-1", redirect_uri=REDIRECT, scopes=("daily",), now=T0
    )
    result = service.complete_link(
        state=_state_from(redirect.authorize_url), code="c", redirect_uri=REDIRECT, now=T0
    )
    assert result.connection is not None

    assert service.mark_needs_reauth(connection_id=result.connection.connection_id, now=T0)

    with factory() as session:
        row = session.get(Connection, result.connection.connection_id)
        assert row is not None
        assert row.status == ConnectionStatus.NEEDS_REAUTH.value


def test_mark_needs_reauth_on_unknown_connection_returns_false(
    factory: sessionmaker[Session],
) -> None:
    service = _service(factory, _token_ok)
    assert service.mark_needs_reauth(connection_id="missing", now=T0) is False


def test_start_link_requires_tenant(factory: sessionmaker[Session]) -> None:
    service = _service(factory, _token_ok)
    with pytest.raises(ValueError, match="tenant_id must be non-empty"):
        service.start_link(tenant_id="", redirect_uri=REDIRECT, scopes=("daily",), now=T0)
