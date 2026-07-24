"""End-to-end coverage of the Google Health push-webhook path over real HTTP.

Google Health authenticates its webhook with a Google-signed OIDC token in the
Authorization header. A real RSA key signs the token; the route's verifier is
patched to use a fake JWK client returning the matching public key, so the full
signature + claim + record + enqueue flow runs without network access.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import akunaki.api.routes.webhooks as webhooks_mod
from akunaki.adapters.connectors.google_push_verifier import GooglePushVerifier
from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Job, Tenant, WebhookInbox
from akunaki.api.app import create_app
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import INCREMENTAL_SYNC_JOB_TYPE, to_utc_rfc3339

NOW_S = to_utc_rfc3339(datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC))
KEK = b"\x55" * KEY_BYTES
KEK_B64 = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="  # 32 bytes
AUD = "https://api.example.com/webhooks/google_health/conn-g"
SA = "push@project.iam.gserviceaccount.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        secret_keks=f"v1:{KEK_B64}",
        active_kek_version="v1",
        google_health_push_audience=AUD,
        google_health_push_service_account=SA,
    )


class _FakeSigningKey:
    def __init__(self, public_key: Any) -> None:
        self.key = public_key


class _FakeJWKClient:
    def __init__(self, public_key: Any) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._public_key)


def _bearer(**overrides: object) -> str:
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": AUD,
        "exp": 9999999999,
        "email": SA,
        "email_verified": True,
    }
    claims.update(overrides)
    priv = _KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, priv, algorithm="RS256")


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "webhook_google.db"
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
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-g",
        tenant_id="tenant-1",
        provider=Provider.GOOGLE_HEALTH,
        sealed_secret=sealer.seal(b'{"access_token":"at"}', aad=b"conn-g"),
        scopes=("health.sleep.read",),
        external_user_id=None,
        now=datetime.now(UTC),
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def client(route_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Patch the route's verifier to use the fake JWK client (no network).
    def _patched(*, expected_audience: str, expected_service_account: str) -> GooglePushVerifier:
        return GooglePushVerifier(
            expected_audience=expected_audience,
            expected_service_account=expected_service_account,
            jwk_client=_FakeJWKClient(_KEY.public_key()),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(webhooks_mod, "GooglePushVerifier", _patched)
    yield TestClient(create_app(_settings(route_db)))


def test_valid_google_push_records_and_enqueues(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    resp = client.post(
        "/webhooks/google_health/conn-g",
        content=b'{"message":{"data":"..."}}',
        headers={"Authorization": f"Bearer {_bearer()}", "x-delivery-id": "g-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    with factory() as session:
        assert len(session.scalars(select(WebhookInbox)).all()) == 1
        assert (
            len(session.scalars(select(Job).where(Job.job_type == INCREMENTAL_SYNC_JOB_TYPE)).all())
            == 1
        )


def test_wrong_service_account_is_401(client: TestClient, factory: sessionmaker[Session]) -> None:
    resp = client.post(
        "/webhooks/google_health/conn-g",
        content=b"{}",
        headers={"Authorization": f"Bearer {_bearer(email='attacker@evil.example.com')}"},
    )
    assert resp.status_code == 401
    with factory() as session:
        assert session.scalars(select(WebhookInbox)).all() == []


def test_missing_bearer_is_401(client: TestClient) -> None:
    resp = client.post("/webhooks/google_health/conn-g", content=b"{}")
    assert resp.status_code == 401


def test_google_health_404_when_unconfigured(route_db: str) -> None:
    # No push audience/service account configured -> the path is disabled (404).
    unconfigured = TestClient(
        create_app(
            Settings(database_url=route_db, secret_keks=f"v1:{KEK_B64}", active_kek_version="v1")
        )
    )
    resp = unconfigured.post(
        "/webhooks/google_health/conn-g",
        content=b"{}",
        headers={"Authorization": f"Bearer {_bearer()}"},
    )
    assert resp.status_code == 404
