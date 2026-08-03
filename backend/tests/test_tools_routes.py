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

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import FactRecord, SleepSession, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.anomalies import AnomalySeverity
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.workout_normalizer import WorkoutFact

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
    cfg.set_main_option("script_location", str(_backend_root() / "src" / "akunaki" / "migrations"))
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


# ---------------------------------------------------------------------------
# Canonical registry: anomalies, workouts, connections
# ---------------------------------------------------------------------------


def _seed_workout(factory: sessionmaker[Session], *, workout_id: str, start_hour: int) -> None:
    FactRepository(factory).write_workout_fact(
        fact_record_id=workout_id,
        tenant_id="tenant-1",
        connection_id=None,
        fact=WorkoutFact(
            vendor_record_id=workout_id,
            start_utc=f"{DAY}T{start_hour:02d}:00:00Z",
            end_utc=f"{DAY}T{start_hour + 1:02d}:00:00Z",
            local_health_day=DAY,
            source_offset_minutes=0,
            session_load=120.0,
            zone1_min=10.0,
            zone2_min=20.0,
            zone3_min=15.0,
            zone4_min=5.0,
            zone5_min=0.0,
            quality="high",
            confidence=1.0,
            content_hash=workout_id,
        ),
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="polar.v1",
        now=T0,
    )


def test_lists_the_canonical_read_tools(client: TestClient, factory: sessionmaker[Session]) -> None:
    """The registry names the read tools the canonical catalog specifies."""
    _login(client, factory)
    names = {t["name"] for t in client.get("/v1/tools").json()["tools"]}

    assert {
        "health.find_anomalies",
        "health.get_recent_workouts",
        "health.get_workout",
        "connections.list",
    } <= names


def test_connections_tool_is_not_scoped_as_health(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Connection metadata is not health data; the scope must not over-grant."""
    _login(client, factory)
    tools = client.get("/v1/tools").json()["tools"]
    connections = next(t for t in tools if t["name"] == "connections.list")

    assert connections["scopes"] == ["read:connections"]
    assert connections["sensitivity"] == "low"
    assert connections["side_effect"] == "none"


def test_no_mutating_tool_is_registered(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Only read tools ship: agent-mutation confirmation does not exist yet.

    Registering ``connections.sync`` or ``privacy.delete`` before the one-time
    expiring confirmation machinery exists would make a mutation invocable on
    the honour system.
    """
    _login(client, factory)
    tools = client.get("/v1/tools").json()["tools"]

    assert all(t["side_effect"] == "none" for t in tools)
    names = {t["name"] for t in tools}
    assert "connections.sync" not in names
    assert "privacy.delete" not in names


def test_invoke_find_anomalies(client: TestClient, factory: sessionmaker[Session]) -> None:
    AnomalyRepository(factory).open_interval(
        anomaly_id="an-1",
        tenant_id="tenant-1",
        feature_code="low_hrv",
        severity=AnomalySeverity.HIGH,
        z_like=-3.0,
        formula_version="general_recovery_v0.1.0",
        local_health_day=DAY,
        now=T0,
    )
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/health.find_anomalies",
        json={"input": {"day": DAY}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert [a["feature_code"] for a in body["anomalies"]] == ["low_hrv"]
    # The detector's internal z stays out of a model's context.
    assert "z_like" not in body["anomalies"][0]


def test_invoke_get_recent_workouts(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_workout(factory, workout_id="w1", start_hour=6)
    _seed_workout(factory, workout_id="w2", start_hour=18)
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/health.get_recent_workouts",
        json={"input": {"limit": 1}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert [w["workout_id"] for w in body["workouts"]] == ["w2"]
    assert body["next_cursor"] is not None
    assert body["workouts"][0]["total_zone_min"] == 50.0


def test_invoke_get_workout(client: TestClient, factory: sessionmaker[Session]) -> None:
    _seed_workout(factory, workout_id="w1", start_hour=6)
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/health.get_workout",
        json={"input": {"workout_id": "w1"}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    assert body["workout_id"] == "w1"
    assert body["session_load"] == 120.0


def test_unknown_workout_through_a_tool_is_404(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A tool must not become a way to probe ids for existence."""
    csrf = _login(client, factory)

    response = client.post(
        "/v1/tools/health.get_workout",
        json={"input": {"workout_id": "nope"}},
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "not_found"}


def test_invoke_connections_list(client: TestClient, factory: sessionmaker[Session]) -> None:
    csrf = _login(client, factory)

    body = client.post(
        "/v1/tools/connections.list",
        json={"input": {}},
        headers={CSRF_HEADER_NAME: csrf},
    ).json()

    # No connector linked in this fixture: an empty list, not an error.
    assert body == {"connections": []}
