"""End-to-end coverage of ``/v1/tools`` over real HTTP.

The typed registry is exposed to a plain HTTP client with no model packages
involved: list the tools, then invoke one under the session context. The tenant
comes from the session, and CSRF is enforced on the POST invoke path.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import FactRecord, SleepSession, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-22"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "tools_routes.db"
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
    engine = create_db_engine(Settings(database_url=route_db))
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
def client(route_db: str) -> TestClient:
    return TestClient(create_app(Settings(database_url=route_db)))


def _login(client: TestClient, factory: sessionmaker[Session]) -> str:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)
    return issued.csrf_secret


def _seed_sleep(factory: sessionmaker[Session], *, day: str, fact_id: str) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider="oura",
                entity_type="sleep_session",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=NOW_S,
                start_utc=NOW_S,
                end_utc=NOW_S,
                source_offset_minutes=0,
                iana_timezone="UTC",
                local_health_day=day,
                unit=None,
                quality="high",
                confidence=1.0,
                freshness_at=NOW_S,
                raw_revision_id=None,
                raw_payload_id=None,
                schema_version="v1",
                normalizer_version="sleep_v0.1.0",
                content_hash=fact_id,
                fact_key=f"sleep_session:{fact_id}",
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )
        session.add(
            SleepSession(
                fact_record_id=fact_id,
                tenant_id="tenant-1",
                is_nap=0,
                duration_min=420.0,
                time_in_bed_min=None,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def test_list_requires_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.get("/v1/tools").status_code == 401


def test_lists_the_health_tools(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    body = client.get("/v1/tools").json()
    names = {t["name"] for t in body["tools"]}
    assert {"health.get_today", "health.get_recovery", "health.get_sleep"} <= names
    recovery = next(t for t in body["tools"] if t["name"] == "health.get_recovery")
    assert recovery["side_effect"] == "none"
    assert recovery["sensitivity"] == "health_read"
    assert "read:health" in recovery["scopes"]


def test_invoke_requires_csrf(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)  # cookie only, no CSRF header
    response = client.post("/v1/tools/health.get_sleep", json={"input": {"day": DAY}})
    assert response.status_code == 403


def test_invoke_sleep_tool(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_sleep(factory, day=DAY, fact_id="s1")
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.get_sleep",
        json={"input": {"day": DAY}},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["duration_min"] == 420.0
    assert body["formula_version"] == "sleep_summary_v0.1.0"


def test_unknown_tool_is_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.nope", json={"input": {}}, headers={CSRF_HEADER_NAME: csrf}
    )
    assert response.status_code == 404


def test_malformed_tool_input_is_422(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.get_sleep",
        json={"input": {"day": "2026-13-40"}},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 422


def test_invoke_today_tool(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_sleep(factory, day=DAY, fact_id="s1")
    csrf = _login(client, factory)
    response = client.post(
        "/v1/tools/health.get_today",
        json={"input": {"day": DAY}},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert response.status_code == 200
    body = response.json()
    # A sleep-only tenant: recovery insufficient -> training label insufficient.
    assert body["training_label"] == "insufficient"
    assert body["ruleset_version"] == "training_label_v0.1.0"
