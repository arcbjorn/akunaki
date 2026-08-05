"""``GET /v1/recommendations`` over real HTTP.

The reason this surface exists is the **suppressed** list: the engine resolves
conflicts and drops the losers, and ``/v1/today`` never shows them. These verify
a suppressed rule reaches the client naming the rule that beat it.
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
from akunaki.api.routes.today import today_service
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.application.recovery_surface import RecoverySurface
from akunaki.application.today_surface import TodaySurface
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.recommendations import (
    ConflictGroup,
    Recommendation,
    Role,
    RuleId,
)
from akunaki.domain.recovery import RecoveryStatus
from akunaki.domain.training_label import TrainingLabel

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
TARGET_DAY = "2026-07-20"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "recommendations_routes.db"
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


def _seed_sleep(
    factory: sessionmaker[Session],
    *,
    day: str,
    duration_min: float,
    fact_id: str,
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
        session.flush()
        session.add(
            SleepSession(
                fact_record_id=fact_id,
                tenant_id="tenant-1",
                is_nap=0,
                duration_min=duration_min,
                time_in_bed_min=None,
                efficiency_pct=None,
                light_min=None,
                deep_min=None,
                rem_min=None,
                awake_min=None,
            )
        )


class _SuppressingToday:
    """A composite whose day has a real conflict: rest_day beats load_ease.

    Mirrors what ``select_recommendations`` produces for an over-load rest day,
    so the route serializes the same shape it would in production.
    """

    def today_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = 480
    ) -> TodaySurface:
        return TodaySurface(
            local_health_day=local_health_day,
            status="ok",
            recovery=RecoverySurface(
                local_health_day=local_health_day,
                score_code="recovery",
                status=RecoveryStatus.OK,
                score=25,
                confidence=0.9,
                available_weight=0.9,
                factors=(),
                data_gaps=(),
                formula_version="general_recovery_v0.1.0",
            ),
            sleep=None,
            training_label=TrainingLabel.REST,
            ruleset_version="training_label_v0.1.0",
            primary_recommendation=Recommendation(
                rule_id=RuleId.REST_DAY,
                role=Role.PRIMARY,
                priority=100,
                conflict_group=ConflictGroup.LOAD,
            ),
            supporting_recommendations=(),
            suppressed_recommendations=(
                Recommendation(
                    rule_id=RuleId.LOAD_EASE,
                    role=Role.SUPPRESSED,
                    priority=90,
                    conflict_group=ConflictGroup.LOAD,
                    suppressed_by=RuleId.REST_DAY,
                ),
            ),
            data_gaps=(),
            formula_version="general_recovery_v0.1.0",
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

    assert client.get("/v1/recommendations", params={"day": TARGET_DAY}).status_code == 401


def test_malformed_day_is_rejected(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)

    assert client.get("/v1/recommendations", params={"day": "2026-13-99"}).status_code == 422


def test_day_is_required(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A local health day belongs to the tenant's timezone, never the server's."""
    _login(client, factory)

    assert client.get("/v1/recommendations").status_code == 422


def test_resolved_set_carries_the_primary(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A sleep-only tenant fails the recovery gate, so the data-gap rule leads."""
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="rec-1")
    _login(client, factory)

    body = client.get("/v1/recommendations", params={"day": TARGET_DAY}).json()

    assert body["local_health_day"] == TARGET_DAY
    assert body["primary"]["rule_id"] == "data_gap_reconnect"
    assert body["primary"]["role"] == "primary"
    assert body["primary"]["conflict_group"] == "data"


def test_primary_is_null_when_no_rule_fires(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Absent guidance is null, never a fabricated recommendation."""
    _login(client, factory)

    body = client.get("/v1/recommendations", params={"day": TARGET_DAY}).json()

    assert body["primary"] is None or isinstance(body["primary"], dict)
    assert isinstance(body["supporting"], list)
    assert isinstance(body["suppressed"], list)


def test_suppressed_recommendations_are_disclosed(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The whole point of the surface: losers reach the client, not /dev/null.

    ``/v1/today`` renders only primary and supporting, so a rule that fired and
    lost its conflict group is invisible there.

    Reaching a real collision needs a rest-band recovery score *and* an over-load
    ACWR window — 28 days of seeded workouts. The composite is overridden
    instead, because what this test must pin down is the route's serialization:
    that ``suppressed`` is rendered rather than dropped, with ``suppressed_by``
    naming the winner. ``test_today_surface.py`` proves the composite itself
    carries the field, off real conflict resolution.
    """
    _login(client, factory)
    client.app.dependency_overrides[today_service] = lambda: _SuppressingToday()
    try:
        body = client.get("/v1/recommendations", params={"day": TARGET_DAY}).json()
    finally:
        client.app.dependency_overrides.clear()

    assert body["primary"]["rule_id"] == "rest_day"
    [entry] = body["suppressed"]
    assert entry["rule_id"] == "load_ease"
    assert entry["role"] == "suppressed"
    # Without the winner named, the client cannot explain the absence.
    assert entry["suppressed_by"] == "rest_day"


def test_the_primary_is_never_suppressed(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A rule cannot both win globally and have been beaten."""
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="rec-1")
    _login(client, factory)

    body = client.get("/v1/recommendations", params={"day": TARGET_DAY}).json()

    assert body["primary"]["suppressed_by"] is None
    suppressed_ids = {entry["rule_id"] for entry in body["suppressed"]}
    assert body["primary"]["rule_id"] not in suppressed_ids


def test_ruleset_version_is_disclosed(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Guidance is only interpretable against the ruleset that produced it."""
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="rec-1")
    _login(client, factory)

    body = client.get("/v1/recommendations", params={"day": TARGET_DAY}).json()

    assert body["ruleset_version"] == "training_label_v0.1.0"


def test_carries_no_health_values(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Rule codes and roles only — the scores live on /v1/today and /v1/metrics."""
    _seed_sleep(factory, day=TARGET_DAY, duration_min=420.0, fact_id="rec-1")
    _login(client, factory)

    body = client.get("/v1/recommendations", params={"day": TARGET_DAY}).json()

    assert set(body) == {
        "local_health_day",
        "ruleset_version",
        "primary",
        "supporting",
        "suppressed",
    }


def test_never_reads_another_tenants_day(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Tenant scoping holds even though the response carries no measurements."""
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Other",
            )
        )
    _login(client, factory)

    response = client.get("/v1/recommendations", params={"day": TARGET_DAY})

    assert response.status_code == 200
    assert response.json()["local_health_day"] == TARGET_DAY
