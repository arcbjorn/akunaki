"""``GET /v1/sync/status`` over real HTTP.

Answers what ``/v1/connections`` cannot: **history**. A failure counter says a
connection failed three times; only a run record says when, on which stream, and
whether the most recent attempt succeeded.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Connection, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.adapters.db.sync_run_repository import SyncRunRepository
from akunaki.api.app import create_app
from akunaki.api.security import SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.sync_runs import SyncRunStatus, SyncRunTrigger
from conftest import upgrade_to_head

T0 = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "sync_status.db"
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
        session.add(
            Connection(
                id="conn-1",
                tenant_id="tenant-1",
                provider="oura",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
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


def _record(
    factory: sessionmaker[Session],
    *,
    run_id: str = "run-1",
    status: SyncRunStatus | None = SyncRunStatus.SUCCEEDED,
    trigger: SyncRunTrigger = SyncRunTrigger.SCHEDULE,
    error_class: str | None = None,
    new_revisions: int = 1,
    now: datetime = T0,
) -> None:
    """Open a run, and close it unless ``status`` is None (left unsettled)."""
    repo = SyncRunRepository(factory)
    repo.open(
        run_id=run_id,
        tenant_id="tenant-1",
        connection_id="conn-1",
        trigger=trigger,
        stream="sleep",
        now=now,
    )
    if status is not None:
        repo.close(
            run_id=run_id,
            status=status,
            now=now,
            stats={"new_revisions": new_revisions},
            error_class=error_class,
        )


def _runs(client: TestClient) -> list[dict[str, object]]:
    runs: list[dict[str, object]] = client.get("/v1/sync/status").json()["runs"]
    return runs


def test_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))

    assert client.get("/v1/sync/status").status_code == 401


def test_no_runs_is_a_real_answer(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A tenant that has never synced has no runs; that is not an error."""
    _login(client, factory)

    assert client.get("/v1/sync/status").json() == {"runs": []}


def test_a_successful_run_is_reported(client: TestClient, factory: sessionmaker[Session]) -> None:
    _record(factory)
    _login(client, factory)

    [run] = _runs(client)

    assert run["status"] == "succeeded"
    assert run["provider"] == "oura"
    assert run["stream"] == "sleep"
    assert run["finished_at"] is not None
    assert run["error_class"] is None


def test_a_failure_reports_when_and_why(client: TestClient, factory: sessionmaker[Session]) -> None:
    """What a failure *counter* cannot tell you."""
    _record(factory, status=SyncRunStatus.FAILED, error_class="TransientJobError")
    _login(client, factory)

    [run] = _runs(client)

    assert run["status"] == "failed"
    assert run["error_class"] == "TransientJobError"
    assert run["started_at"] == NOW_S


def test_an_unsettled_run_is_visible(client: TestClient, factory: sessionmaker[Session]) -> None:
    """A worker that dies mid-run leaves the row it opened.

    Reporting it as ``running`` with a null ``finished_at`` is more honest than
    the alternative, which is no record at all.
    """
    _record(factory, status=None)
    _login(client, factory)

    [run] = _runs(client)

    assert run["status"] == "running"
    assert run["finished_at"] is None


def test_a_run_reports_what_it_ingested(client: TestClient, factory: sessionmaker[Session]) -> None:
    """``stats_json`` is 'counts only' per the schema and was always NULL."""
    _record(factory, new_revisions=7)
    _login(client, factory)

    [run] = _runs(client)

    assert run["new_revisions"] == 7


def test_an_unsettled_run_reports_no_count(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Null, not zero: a run that never finished ingested an unknown amount."""
    _record(factory, status=None)
    _login(client, factory)

    [run] = _runs(client)

    assert run["new_revisions"] is None


def test_runs_are_newest_first(client: TestClient, factory: sessionmaker[Session]) -> None:
    _record(factory, run_id="older", now=T0)
    _record(factory, run_id="newer", now=T0 + timedelta(minutes=5))
    _login(client, factory)

    assert [run["run_id"] for run in _runs(client)] == ["newer", "older"]


def test_the_limit_is_honored(client: TestClient, factory: sessionmaker[Session]) -> None:
    for index in range(5):
        _record(factory, run_id=f"run-{index}", now=T0 + timedelta(minutes=index))
    _login(client, factory)

    body = client.get("/v1/sync/status", params={"limit": 2}).json()

    assert len(body["runs"]) == 2


def test_an_out_of_range_limit_is_rejected(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    _login(client, factory)

    assert client.get("/v1/sync/status", params={"limit": 0}).status_code == 422
    assert client.get("/v1/sync/status", params={"limit": 1000}).status_code == 422


def test_never_reports_another_tenants_runs(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
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
        session.add(
            Connection(
                id="conn-2",
                tenant_id="tenant-2",
                provider="polar",
                status="active",
                scopes_granted_json="[]",
                external_user_id=None,
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )
    SyncRunRepository(factory).open(
        run_id="theirs",
        tenant_id="tenant-2",
        connection_id="conn-2",
        trigger=SyncRunTrigger.SCHEDULE,
        stream="workout",
        now=T0,
    )
    _login(client, factory)

    assert _runs(client) == []


def test_carries_no_health_values(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Transport history, never measurements."""
    _record(factory)
    _login(client, factory)

    [run] = _runs(client)

    assert set(run) == {
        "new_revisions",
        "run_id",
        "connection_id",
        "provider",
        "trigger",
        "stream",
        "status",
        "started_at",
        "finished_at",
        "error_class",
    }
