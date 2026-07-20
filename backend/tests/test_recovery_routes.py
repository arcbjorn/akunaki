"""End-to-end coverage of ``/v1/recovery`` over real HTTP.

The whole assembled scoring path runs behind a real authenticated request. For
any current (sleep-only) tenant the honest outcome is ``insufficient`` with a
null score and disclosed data gaps; the tests pin that so a future regression
that fabricates a score is caught. Facts are seeded as ORM rows.
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
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
TARGET_DAY = "2026-07-20"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "recovery_routes.db"
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


def _seed_sleep(
    factory: sessionmaker[Session],
    *,
    day: str,
    duration_min: float,
    fact_id: str,
    time_in_bed_min: float | None = None,
) -> None:
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
                duration_min=duration_min,
                time_in_bed_min=time_in_bed_min,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def _login(client: TestClient, factory: sessionmaker[Session]) -> None:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    response = client.get("/v1/recovery", params={"day": TARGET_DAY})
    assert response.status_code == 401


def test_sleep_only_tenant_is_insufficient(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="today")
    _login(client, factory)

    response = client.get("/v1/recovery", params={"day": TARGET_DAY})
    assert response.status_code == 200
    body = response.json()
    assert body["score_code"] == "recovery"
    assert body["status"] == "insufficient"
    assert body["score"] is None
    assert body["confidence"] == 0.0
    assert body["formula_version"] == "general_recovery_v0.1.0"
    gap_codes = {g["code"] for g in body["data_gaps"]}
    assert "missing_hrv_or_resting_hr" in gap_codes


def test_no_data_reports_missing_sleep(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    body = client.get("/v1/recovery", params={"day": TARGET_DAY}).json()
    assert body["status"] == "insufficient"
    assert body["score"] is None
    gap_codes = {g["code"] for g in body["data_gaps"]}
    assert "missing_authoritative_sleep" in gap_codes


def test_factors_only_list_present_contributors(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="today")
    _login(client, factory)
    body = client.get("/v1/recovery", params={"day": TARGET_DAY}).json()
    factor_codes = {f["factor_code"] for f in body["factors"]}
    # Only sleep adherence is present today; nothing else may appear.
    assert factor_codes == {"sleep_adherence"}


def test_malformed_day_is_rejected(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    response = client.get("/v1/recovery", params={"day": "2026-13-40"})
    assert response.status_code == 422


def test_response_never_carries_a_fabricated_score(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # Guard the cardinal rule at the HTTP boundary: an insufficient recovery
    # must expose a null score, not a midpoint.
    _seed_sleep(factory, day=TARGET_DAY, duration_min=200.0, fact_id="today")
    _login(client, factory)
    body = client.get("/v1/recovery", params={"day": TARGET_DAY}).json()
    assert body["score"] is None
