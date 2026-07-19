"""End-to-end OIDC login: begin, callback, and session issuance.

Drives the real login service and routes against a real RSA-signed id_token
served by an in-process mock transport. This is the flow that finally makes
``/v1`` reachable, so the security orderings — state before redirect, single
use, verify before session — are the point.
"""

from __future__ import annotations

import base64
import itertools
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWK, PyJWKClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.crypto.oauth import (
    code_challenge_s256,
    generate_code_verifier,
    generate_nonce,
    generate_state,
)
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.login_state_repository import LoginStateRepository
from akunaki.adapters.db.models import Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.adapters.db.user_repository import UserRepository
from akunaki.adapters.oidc.client import OIDCClient
from akunaki.application.login import LoginRejection, LoginService
from akunaki.config import Settings, clear_settings_cache

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
ISSUER = "https://auth.example.com"
CLIENT_ID = "akunaki-web"
CLIENT_SECRET = "csecret"
REDIRECT = "https://app.example.com/auth/callback"
KEK = b"\xaa" * KEY_BYTES
KID = "k1"

_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_IDS = itertools.count(1)


def _jwks() -> dict[str, object]:
    pn = _SIGNING_KEY.public_key().public_numbers()

    def b64(v: int) -> str:
        length = (v.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(v.to_bytes(length, "big")).decode().rstrip("=")

    return {
        "keys": [
            {"kty": "RSA", "use": "sig", "kid": KID, "alg": "RS256", "n": b64(pn.n), "e": b64(pn.e)}
        ]
    }


class _StaticJWKClient(PyJWKClient):
    def get_signing_key_from_jwt(self, token: str):  # type: ignore[no-untyped-def]
        [jwk] = _jwks()["keys"]  # type: ignore[index]
        return PyJWK.from_dict(jwk)


def _id_token(nonce: str, **overrides: object) -> str:
    epoch = int(NOW.timestamp())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "oidc-subject-1",
        "nonce": nonce,
        "exp": epoch + 300,
        "iat": epoch - 5,
        "email": "person@example.com",
    }
    claims.update(overrides)
    return jwt.encode(claims, _SIGNING_KEY, algorithm="RS256", headers={"kid": KID})


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def login_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "login.db"
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
def factory(login_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=login_db))
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _token_handler(
    id_token_for: Callable[[str], str],
    captured_nonce: list[str],
) -> Callable[[httpx2.Request], httpx2.Response]:
    """Serve discovery, JWKS, and a token whose nonce echoes the request's."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/openid-configuration"):
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path.endswith("/jwks"):
            return httpx2.Response(200, json=_jwks())
        if path.endswith("/token"):
            nonce = captured_nonce[-1]
            return httpx2.Response(
                200, json={"id_token": id_token_for(nonce), "token_type": "Bearer"}
            )
        return httpx2.Response(404)

    return handler


def _service(
    factory: sessionmaker[Session],
    handler: Callable[[httpx2.Request], httpx2.Response],
    captured_nonce: list[str],
) -> LoginService:
    """A login service whose authorize URL records the nonce it issued."""
    client = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx2.Client(transport=httpx2.MockTransport(handler)),
        jwk_client=_StaticJWKClient(f"{ISSUER}/jwks"),
    )
    ids = (f"id-{n}" for n in _IDS)

    def recording_nonce() -> str:
        nonce = generate_nonce()
        captured_nonce.append(nonce)
        return nonce

    return LoginService(
        client=client,
        states=LoginStateRepository(factory),
        users=UserRepository(factory),
        sessions=SessionRepository(factory),
        sealer=EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1"),
        generate_state=generate_state,
        generate_nonce=recording_nonce,
        generate_code_verifier=generate_code_verifier,
        code_challenge=code_challenge_s256,
        new_id=lambda: next(ids),
    )


def _begin_and_get_state(service: LoginService) -> str:
    from urllib.parse import parse_qs, urlparse

    redirect = service.begin(redirect_uri=REDIRECT, now=NOW)
    return parse_qs(urlparse(redirect.authorize_url).query)["state"][0]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_full_login_provisions_a_user_and_issues_a_session(
    factory: sessionmaker[Session],
) -> None:
    nonce: list[str] = []
    service = _service(factory, _token_handler(_id_token, nonce), nonce)

    state = _begin_and_get_state(service)
    result = service.complete(state=state, code="auth-code", redirect_uri=REDIRECT, now=NOW)

    assert result.ok
    assert result.session_token is not None
    assert result.csrf_secret is not None

    with factory() as session:
        user = session.scalars(select(User)).one()
        tenant = session.scalars(select(Tenant)).one()
    assert user.oidc_issuer == ISSUER
    assert user.oidc_subject == "oidc-subject-1"
    assert user.tenant_id == tenant.id
    assert result.tenant_id == tenant.id

    # The issued session actually authenticates.
    validation = SessionRepository(factory).validate(token=result.session_token, now=NOW)
    assert validation.ok
    assert validation.session is not None
    assert validation.session.user_id == user.id


def test_returning_login_reuses_the_existing_user(
    factory: sessionmaker[Session],
) -> None:
    """A second login for the same subject must not create a second user."""
    nonce: list[str] = []
    service = _service(factory, _token_handler(_id_token, nonce), nonce)

    first_state = _begin_and_get_state(service)
    first = service.complete(state=first_state, code="c1", redirect_uri=REDIRECT, now=NOW)

    second_state = _begin_and_get_state(service)
    second = service.complete(state=second_state, code="c2", redirect_uri=REDIRECT, now=NOW)

    assert first.ok and second.ok
    assert first.user_id == second.user_id
    assert first.tenant_id == second.tenant_id
    with factory() as session:
        assert len(session.scalars(select(User)).all()) == 1
        assert len(session.scalars(select(Tenant)).all()) == 1


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_replayed_state_is_rejected(factory: sessionmaker[Session]) -> None:
    nonce: list[str] = []
    service = _service(factory, _token_handler(_id_token, nonce), nonce)
    state = _begin_and_get_state(service)

    first = service.complete(state=state, code="c", redirect_uri=REDIRECT, now=NOW)
    second = service.complete(state=state, code="c", redirect_uri=REDIRECT, now=NOW)

    assert first.ok
    assert second.rejection is LoginRejection.INVALID_STATE


def test_forged_state_is_rejected(factory: sessionmaker[Session]) -> None:
    nonce: list[str] = []
    service = _service(factory, _token_handler(_id_token, nonce), nonce)
    _begin_and_get_state(service)

    result = service.complete(state=generate_state(), code="c", redirect_uri=REDIRECT, now=NOW)
    assert result.rejection is LoginRejection.INVALID_STATE


def test_mismatched_redirect_is_rejected(factory: sessionmaker[Session]) -> None:
    nonce: list[str] = []
    service = _service(factory, _token_handler(_id_token, nonce), nonce)
    state = _begin_and_get_state(service)

    result = service.complete(state=state, code="c", redirect_uri="https://evil.test/cb", now=NOW)
    assert result.rejection is LoginRejection.INVALID_STATE


def test_replayed_id_token_nonce_is_rejected(factory: sessionmaker[Session]) -> None:
    """A token whose nonce is from a different login must not authenticate."""
    nonce: list[str] = []

    def wrong_nonce_token(_nonce: str) -> str:
        return _id_token(generate_nonce())  # a nonce we never issued

    service = _service(factory, _token_handler(wrong_nonce_token, nonce), nonce)
    state = _begin_and_get_state(service)

    result = service.complete(state=state, code="c", redirect_uri=REDIRECT, now=NOW)
    assert result.rejection is LoginRejection.TOKEN_REJECTED
    # No user is provisioned for a rejected token.
    with factory() as session:
        assert session.scalars(select(User)).all() == []


def test_wrong_audience_token_is_rejected(factory: sessionmaker[Session]) -> None:
    nonce: list[str] = []

    def wrong_aud(nonce_value: str) -> str:
        return _id_token(nonce_value, aud="some-other-client")

    service = _service(factory, _token_handler(wrong_aud, nonce), nonce)
    state = _begin_and_get_state(service)

    result = service.complete(state=state, code="c", redirect_uri=REDIRECT, now=NOW)
    assert result.rejection is LoginRejection.TOKEN_REJECTED


def test_token_endpoint_failure_is_a_provider_error(
    factory: sessionmaker[Session],
) -> None:
    nonce: list[str] = []

    def failing(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/token"):
            return httpx2.Response(400, json={"error": "invalid_grant"})
        return _token_handler(_id_token, nonce)(request)

    service = _service(factory, failing, nonce)
    state = _begin_and_get_state(service)

    result = service.complete(state=state, code="c", redirect_uri=REDIRECT, now=NOW)
    assert result.rejection is LoginRejection.PROVIDER_ERROR


def test_no_session_when_login_fails(factory: sessionmaker[Session]) -> None:
    """A rejected login must issue no session."""
    nonce: list[str] = []
    service = _service(factory, _token_handler(_id_token, nonce), nonce)
    _begin_and_get_state(service)

    service.complete(state=generate_state(), code="c", redirect_uri=REDIRECT, now=NOW)

    from akunaki.adapters.db.models import SessionRow

    with factory() as session:
        assert session.scalars(select(SessionRow)).all() == []


def test_missing_code_does_not_consume_the_state(
    factory: sessionmaker[Session],
) -> None:
    nonce: list[str] = []
    service = _service(factory, _token_handler(_id_token, nonce), nonce)
    state = _begin_and_get_state(service)

    denied = service.complete(state=state, code="", redirect_uri=REDIRECT, now=NOW)
    retried = service.complete(state=state, code="c", redirect_uri=REDIRECT, now=NOW)

    assert denied.rejection is LoginRejection.INVALID_STATE
    assert retried.ok


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


def _app_client(login_db: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient whose app has OIDC configured and a patched OIDCClient."""

    settings = Settings(
        database_url=login_db,
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret=CLIENT_SECRET,
        oidc_redirect_uri=REDIRECT,
        secret_keks=f"v1:{base64.b64encode(KEK).decode()}",
        session_cookie_secure=False,
    )
    from akunaki.api.app import create_app

    return TestClient(create_app(settings))


def test_login_routes_absent_without_oidc_config(login_db: str) -> None:
    """An unconfigured deployment exposes no auth surface."""
    from akunaki.api.app import create_app

    app = create_app(Settings(database_url=login_db))
    assert not [p for p in app.openapi()["paths"] if p.startswith("/auth")]


def test_login_routes_present_when_configured(login_db: str) -> None:
    from akunaki.api.app import create_app

    app = create_app(
        Settings(
            database_url=login_db,
            oidc_issuer=ISSUER,
            oidc_client_id=CLIENT_ID,
            oidc_client_secret=CLIENT_SECRET,
            oidc_redirect_uri=REDIRECT,
        )
    )
    paths = app.openapi()["paths"]
    assert "/auth/login" in paths
    assert "/auth/callback" in paths
