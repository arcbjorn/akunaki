"""``GET /v1/anomalies``: active and recently-cleared wellness flags.

The detectors, persistence, and the training-label downshift all shipped, but
the intervals themselves were never readable: ``/v1/today`` collapsed them to a
single "is any active one high-severity" boolean. These drive the surface that
lets a user see which signal was flagged, and since when.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.anomalies import AnomalySeverity
from akunaki.domain.jobs import to_utc_rfc3339
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-25"
FORMULA = "general_recovery_v0.1.0"


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "anomaly_routes.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(route_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=route_db))
    session_factory = create_session_factory(engine)
    for tenant_id, user_id, subject in (
        ("tenant-1", "user-1", "subject-1"),
        ("tenant-2", "user-2", "subject-2"),
    ):
        with session_factory() as session, session.begin():
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


def _login(client: TestClient, factory: sessionmaker[Session]) -> None:
    issued = SessionRepository(factory).issue(
        session_id="sess-user-1",
        user_id="user-1",
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)


def _open_anomaly(
    factory: sessionmaker[Session],
    *,
    feature_code: str,
    tenant_id: str = "tenant-1",
    severity: AnomalySeverity = AnomalySeverity.HIGH,
    day: str = DAY,
) -> None:
    AnomalyRepository(factory).open_interval(
        anomaly_id=f"an-{tenant_id}-{feature_code}",
        tenant_id=tenant_id,
        feature_code=feature_code,
        severity=severity,
        z_like=-3.0,
        formula_version=FORMULA,
        local_health_day=day,
        now=T0,
    )


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.get("/v1/anomalies", params={"day": DAY}).status_code == 401


def test_no_anomalies_is_an_empty_list(client: TestClient, factory: sessionmaker[Session]) -> None:
    """No flagged signals is the healthy common case, not an error."""
    _login(client, factory)
    response = client.get("/v1/anomalies", params={"day": DAY})

    assert response.status_code == 200
    assert response.json() == {"anomalies": [], "window_days": 14}


def test_active_anomaly_is_disclosed(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    _open_anomaly(factory, feature_code="low_hrv")

    body = client.get("/v1/anomalies", params={"day": DAY}).json()

    assert len(body["anomalies"]) == 1
    row = body["anomalies"][0]
    assert row["feature_code"] == "low_hrv"
    assert row["severity"] == "high"
    assert row["started_on"] == DAY
    assert row["ended_on"] is None
    assert row["is_active"] is True
    assert row["formula_version"] == FORMULA


def test_internal_z_score_is_never_disclosed(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The detector's z is engine bookkeeping, not a user-facing number.

    Anomalies are non-diagnostic flags; a bare z-score against a private
    baseline invites exactly the over-reading that framing exists to avoid.
    """
    _login(client, factory)
    _open_anomaly(factory, feature_code="low_hrv")

    row = client.get("/v1/anomalies", params={"day": DAY}).json()["anomalies"][0]

    assert "z_like" not in row
    assert set(row) == {
        "feature_code",
        "severity",
        "started_on",
        "ended_on",
        "is_active",
        "formula_version",
    }


def test_recently_cleared_anomaly_is_still_listed(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A cleared interval explains why a past day read the way it did."""
    _login(client, factory)
    repository = AnomalyRepository(factory)
    _open_anomaly(factory, feature_code="elevated_rhr", day="2026-07-20")
    repository.close_interval(
        tenant_id="tenant-1",
        feature_code="elevated_rhr",
        local_health_day="2026-07-22",
        now=T0,
    )

    body = client.get("/v1/anomalies", params={"day": DAY}).json()

    assert len(body["anomalies"]) == 1
    row = body["anomalies"][0]
    assert row["is_active"] is False
    assert row["ended_on"] == "2026-07-22"


def test_anomaly_cleared_before_the_window_is_dropped(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)
    repository = AnomalyRepository(factory)
    _open_anomaly(factory, feature_code="short_sleep", day="2026-01-01")
    repository.close_interval(
        tenant_id="tenant-1",
        feature_code="short_sleep",
        local_health_day="2026-01-05",
        now=T0,
    )

    assert client.get("/v1/anomalies", params={"day": DAY}).json()["anomalies"] == []
    # A wide enough window brings it back rather than losing it forever.
    wide = client.get("/v1/anomalies", params={"day": "2026-01-10", "window_days": 30}).json()
    assert len(wide["anomalies"]) == 1


def test_active_anomalies_sort_before_cleared_ones(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """What the user can still act on comes first."""
    _login(client, factory)
    repository = AnomalyRepository(factory)
    _open_anomaly(factory, feature_code="elevated_rhr", day="2026-07-20")
    repository.close_interval(
        tenant_id="tenant-1",
        feature_code="elevated_rhr",
        local_health_day="2026-07-22",
        now=T0,
    )
    _open_anomaly(factory, feature_code="low_hrv")

    body = client.get("/v1/anomalies", params={"day": DAY}).json()

    assert [a["feature_code"] for a in body["anomalies"]] == ["low_hrv", "elevated_rhr"]


def test_never_serves_another_tenants_anomalies(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Tenant comes from the session, so another tenant's flags stay invisible."""
    _login(client, factory)
    _open_anomaly(factory, feature_code="low_hrv", tenant_id="tenant-2")

    body = client.get("/v1/anomalies", params={"day": DAY}).json()

    assert body["anomalies"] == []


def test_malformed_day_is_422(client: TestClient, factory: sessionmaker[Session]) -> None:
    _login(client, factory)
    assert client.get("/v1/anomalies", params={"day": "25-07-2026"}).status_code == 422


def test_window_days_is_bounded(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A caller cannot ask for an unbounded scan."""
    _login(client, factory)
    assert client.get("/v1/anomalies", params={"day": DAY, "window_days": 5000}).status_code == 422
