"""End-to-end coverage of the webhook ingress route over real HTTP.

Verify that a correctly-signed delivery is recorded, acknowledged, and enqueues
an incremental-sync refetch; that a redelivery is deduplicated (no second job);
that a bad signature is a 401; and that an unconfigured provider is a 404. The
signature is computed with the same secret the app is configured with.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Job, Tenant, WebhookInbox
from akunaki.api.app import create_app
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import INCREMENTAL_SYNC_JOB_TYPE, to_utc_rfc3339
from akunaki.domain.webhook_verification import hmac_sha256_hex

T0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x55" * KEY_BYTES
KEK_B64 = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="  # 32 bytes
WEBHOOK_SECRET = "polar-webhook-SECRET"
BODY = b'{"event":"EXERCISE","user_id":555}'


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        secret_keks=f"v1:{KEK_B64}",
        active_kek_version="v1",
        polar_webhook_secret=WEBHOOK_SECRET,
    )


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "webhook_routes.db"
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
    # A linked Polar connection with id "conn-polar".
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-polar",
        tenant_id="tenant-1",
        provider=Provider.POLAR,
        sealed_secret=sealer.seal(b'{"access_token":"at"}', aad=b"conn-polar"),
        scopes=("accesslink.read_all",),
        external_user_id="555",
        now=T0,
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def client(route_db: str) -> TestClient:
    return TestClient(create_app(_settings(route_db)))


def _sig(body: bytes = BODY) -> str:
    return hmac_sha256_hex(secret=WEBHOOK_SECRET, body=body)


def test_signed_delivery_is_recorded_and_enqueues_refetch(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    resp = client.post(
        "/webhooks/polar/conn-polar",
        content=BODY,
        headers={"polar-webhook-signature": _sig(), "x-delivery-id": "d-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    with factory() as session:
        inbox = session.scalars(select(WebhookInbox)).all()
        jobs = session.scalars(select(Job).where(Job.job_type == INCREMENTAL_SYNC_JOB_TYPE)).all()
    assert len(inbox) == 1
    assert inbox[0].processing_status == "enqueued"
    assert inbox[0].dedupe_key == "d-1"
    # A refetch job was queued for the connection.
    assert len(jobs) == 1


def test_redelivery_is_deduplicated(client: TestClient, factory: sessionmaker[Session]) -> None:
    headers = {"polar-webhook-signature": _sig(), "x-delivery-id": "d-1"}
    first = client.post("/webhooks/polar/conn-polar", content=BODY, headers=headers)
    second = client.post("/webhooks/polar/conn-polar", content=BODY, headers=headers)

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"

    with factory() as session:
        inbox = session.scalars(select(WebhookInbox)).all()
        jobs = session.scalars(select(Job).where(Job.job_type == INCREMENTAL_SYNC_JOB_TYPE)).all()
    # Exactly one inbox row and one refetch job despite two deliveries.
    assert len(inbox) == 1
    assert len(jobs) == 1


def test_bad_signature_is_401_and_records_nothing(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    resp = client.post(
        "/webhooks/polar/conn-polar",
        content=BODY,
        headers={"polar-webhook-signature": "sha256=deadbeef"},
    )
    assert resp.status_code == 401
    with factory() as session:
        assert session.scalars(select(WebhookInbox)).all() == []


def test_tampered_body_is_401(client: TestClient, factory: sessionmaker[Session]) -> None:
    # Signature is over BODY, but a different body is sent.
    resp = client.post(
        "/webhooks/polar/conn-polar",
        content=b'{"event":"TAMPERED"}',
        headers={"polar-webhook-signature": _sig()},
    )
    assert resp.status_code == 401


def test_unknown_connection_is_401(client: TestClient) -> None:
    resp = client.post(
        "/webhooks/polar/conn-missing",
        content=BODY,
        headers={"polar-webhook-signature": _sig()},
    )
    # A valid signature but unknown connection is the same 401 (no probing).
    assert resp.status_code == 401


def test_unconfigured_provider_is_404(client: TestClient) -> None:
    # Oura has no webhook secret configured here, and google_health is non-HMAC.
    for provider in ("oura", "google_health", "garmin"):
        resp = client.post(
            f"/webhooks/{provider}/conn-polar",
            content=BODY,
            headers={"x-oura-signature": _sig()},
        )
        assert resp.status_code == 404
