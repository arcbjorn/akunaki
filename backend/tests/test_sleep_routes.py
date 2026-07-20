"""End-to-end coverage of ``/v1/sleep`` over real HTTP.

These exercise the wired surface a browser hits: the tenant is taken from the
session cookie (not a query parameter), unknown days are disclosed rather than
imputed, and the response carries a deterministic summary with no sleep score.
Facts are seeded directly as ORM rows so the test owns the exact window shape.
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

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
TARGET_DAY = "2026-07-19"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "sleep_routes.db"
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
        for tenant_id, user_id, subject in (
            ("tenant-1", "user-1", "subject-1"),
            ("tenant-2", "user-2", "subject-2"),
        ):
            session.add(
                Tenant(
                    id=tenant_id,
                    created_at=NOW_S,
                    status="active",
                    primary_timezone="UTC",
                    display_name="Test",
                )
            )
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    oidc_issuer="https://idp.example.com",
                    oidc_subject=subject,
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
    tenant_id: str,
    local_health_day: str,
    duration_min: float,
    fact_id: str,
    is_nap: bool = False,
    exclude: bool = False,
) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id=tenant_id,
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
                local_health_day=local_health_day,
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
                exclude_from_load=1 if exclude else 0,
                created_at=NOW_S,
            )
        )
        session.add(
            SleepSession(
                fact_record_id=fact_id,
                tenant_id=tenant_id,
                is_nap=1 if is_nap else 0,
                duration_min=duration_min,
                time_in_bed_min=duration_min,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


def _login(client: TestClient, factory: sessionmaker[Session], *, user_id: str) -> None:
    # Issue against the real clock so the session is valid at request time; the
    # request path validates the cookie against ``datetime.now(UTC)``. The
    # seeded facts key off the ``day`` query parameter, not wall-clock time, so
    # the fixed ``T0`` used for seeding is unaffected.
    issued = SessionRepository(factory).issue(
        session_id=f"sess-{user_id}",
        user_id=user_id,
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def test_requires_a_session() -> None:
    # No cookie: the surface must not answer.
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    # An in-memory DB has no schema, but auth is checked before any query, so a
    # missing session is a clean 401 regardless.
    response = client.get("/v1/sleep", params={"day": TARGET_DAY})
    assert response.status_code == 401


def test_full_window_at_target_reports_zero_debt(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    for offset in range(14):
        day = (datetime.fromisoformat(TARGET_DAY) - timedelta(days=offset)).date().isoformat()
        _seed_sleep(
            factory,
            tenant_id="tenant-1",
            local_health_day=day,
            duration_min=480,
            fact_id=f"f-{offset}",
        )
    _login(client, factory, user_id="user-1")

    response = client.get("/v1/sleep", params={"day": TARGET_DAY})
    assert response.status_code == 200
    body = response.json()
    assert body["duration_min"] == 480
    assert body["target_min"] == 480
    assert body["adherence_pct"] == 100.0
    assert body["debt"]["minutes"] == 0.0
    assert body["debt"]["known_days"] == 14
    assert body["debt"]["window_days"] == 14
    assert body["debt"]["status"] == "complete"
    assert body["debt"]["is_lower_bound"] is False
    assert body["debt"]["recommendation_eligible"] is True
    assert body["formula_version"] == "sleep_summary_v0.1.0"
    # No score field of any kind may leak into the surface.
    assert "score" not in body
    assert "score" not in body["debt"]


def test_sparse_history_is_partial_lower_bound(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # Only the target day is known: a disclosed lower bound.
    _seed_sleep(
        factory,
        tenant_id="tenant-1",
        local_health_day=TARGET_DAY,
        duration_min=360,
        fact_id="only-day",
    )
    _login(client, factory, user_id="user-1")

    body = client.get("/v1/sleep", params={"day": TARGET_DAY}).json()
    assert body["duration_min"] == 360
    assert body["adherence_pct"] == pytest.approx(75.0)  # 360/480
    assert body["debt"]["minutes"] == 120.0  # one short day
    assert body["debt"]["known_days"] == 1
    assert body["debt"]["status"] == "partial"
    assert body["debt"]["is_lower_bound"] is True
    assert body["debt"]["recommendation_eligible"] is False


def test_daily_duration_sums_sessions_including_naps(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    # A principal night plus a nap on the same day sum into daily duration.
    _seed_sleep(
        factory,
        tenant_id="tenant-1",
        local_health_day=TARGET_DAY,
        duration_min=420,
        fact_id="night",
    )
    _seed_sleep(
        factory,
        tenant_id="tenant-1",
        local_health_day=TARGET_DAY,
        duration_min=60,
        fact_id="nap",
        is_nap=True,
    )
    _login(client, factory, user_id="user-1")

    body = client.get("/v1/sleep", params={"day": TARGET_DAY}).json()
    assert body["duration_min"] == 480  # 420 + 60
    assert body["adherence_pct"] == 100.0


def test_excluded_facts_do_not_count(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_sleep(
        factory,
        tenant_id="tenant-1",
        local_health_day=TARGET_DAY,
        duration_min=999,
        fact_id="excluded",
        exclude=True,
    )
    _login(client, factory, user_id="user-1")

    body = client.get("/v1/sleep", params={"day": TARGET_DAY}).json()
    # The excluded fact is invisible: the day is unknown, duration reads 0.
    assert body["duration_min"] == 0.0
    assert body["debt"]["known_days"] == 0


def test_tenant_isolation(client: TestClient, factory: sessionmaker[Session]) -> None:
    # tenant-2 has sleep; tenant-1 (the caller) does not. The caller must not
    # see tenant-2's data even though the day matches.
    _seed_sleep(
        factory,
        tenant_id="tenant-2",
        local_health_day=TARGET_DAY,
        duration_min=480,
        fact_id="other-tenant",
    )
    _login(client, factory, user_id="user-1")

    body = client.get("/v1/sleep", params={"day": TARGET_DAY}).json()
    assert body["duration_min"] == 0.0
    assert body["debt"]["known_days"] == 0


def test_malformed_day_is_rejected(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory, user_id="user-1")
    response = client.get("/v1/sleep", params={"day": "2026-13-99"})
    assert response.status_code == 422
