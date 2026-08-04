"""``POST /v1/privacy/delete`` and its status read, over real HTTP.

The pipeline was reachable only from its own unit tests: there was no way for a
user to actually request erasure. These drive the documented endpoints end to
end against a migrated database.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.audit_repository import AuditRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import (
    AuditEventRow,
    DeletionCompletionProof,
    DeletionRequest,
    SubjectiveCheckIn,
    Tenant,
    User,
)
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import create_app
from akunaki.api.security import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-25"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "privacy_routes.db"
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


def _login(
    client: TestClient,
    factory: sessionmaker[Session],
    *,
    session_id: str = "sess-user-1",
    user_id: str = "user-1",
) -> str:
    issued = SessionRepository(factory).issue(
        session_id=session_id,
        user_id=user_id,
        now=datetime.now(UTC),
        ttl=timedelta(hours=12),
    )
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issued.token)
    return issued.csrf_secret


def _add_health_row(factory: sessionmaker[Session], *, tenant_id: str, row_id: str) -> None:
    with factory() as session, session.begin():
        session.add(
            SubjectiveCheckIn(
                id=row_id,
                tenant_id=tenant_id,
                local_health_day=DAY,
                energy_n=0.5,
                stress_n=0.5,
                symptom_burden_n=0.1,
                completed_at=NOW_S,
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                created_at=NOW_S,
            )
        )


def test_delete_requires_a_session() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+libsql:///:memory:")))
    assert client.post("/v1/privacy/delete").status_code == 401


def test_delete_requires_csrf(client: TestClient, factory: sessionmaker[Session]) -> None:
    """Erasure is state-changing, so a bare cookie must not be enough."""
    _login(client, factory)
    assert client.post("/v1/privacy/delete").status_code == 403


def test_delete_erases_the_callers_tenant(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """A 200 means the data is gone, not that erasure was queued."""
    _add_health_row(factory, tenant_id="tenant-1", row_id="ci-1")
    csrf = _login(client, factory)

    response = client.post("/v1/privacy/delete", headers={CSRF_HEADER_NAME: csrf})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["deletion_request_id"]
    assert body["rows_scrubbed"] >= 1
    # Counts only — no identity, no health values leave with the last response.
    assert set(body) == {"deletion_request_id", "status", "rows_scrubbed", "jobs_cancelled"}

    with factory() as session:
        assert session.get(Tenant, "tenant-1") is None
        assert session.get(SubjectiveCheckIn, "ci-1") is None


def test_delete_leaves_other_tenants_intact(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """Tenant comes from the session, so erasure cannot reach anyone else."""
    _add_health_row(factory, tenant_id="tenant-1", row_id="ci-1")
    _add_health_row(factory, tenant_id="tenant-2", row_id="ci-2")
    csrf = _login(client, factory)

    assert client.post("/v1/privacy/delete", headers={CSRF_HEADER_NAME: csrf}).status_code == 200

    with factory() as session:
        assert session.get(Tenant, "tenant-2") is not None
        assert session.get(SubjectiveCheckIn, "ci-2") is not None


def test_delete_writes_a_completion_proof(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The proof outlives the tenant: it is the record that erasure happened."""
    csrf = _login(client, factory)
    request_id = client.post("/v1/privacy/delete", headers={CSRF_HEADER_NAME: csrf}).json()[
        "deletion_request_id"
    ]

    with factory() as session:
        request = session.get(DeletionRequest, request_id)
        proof = session.scalars(
            select(DeletionCompletionProof).where(
                DeletionCompletionProof.deletion_request_id == request_id
            )
        ).one()
    assert request is not None
    assert request.status == "completed"
    assert proof.status == "completed"


def test_session_cannot_be_reused_after_deletion(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The session died with its tenant; the cookie must stop working."""
    csrf = _login(client, factory)
    client.post("/v1/privacy/delete", headers={CSRF_HEADER_NAME: csrf})

    # Whatever the client still holds, no authenticated surface answers.
    assert client.get("/v1/session").status_code == 401


def test_status_reports_a_completed_deletion(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The status handle stays readable once the tenant is gone."""
    csrf = _login(client, factory)
    request_id = client.post("/v1/privacy/delete", headers={CSRF_HEADER_NAME: csrf}).json()[
        "deletion_request_id"
    ]

    response = client.get(f"/v1/privacy/delete/{request_id}")

    assert response.status_code == 200
    body = response.json()
    assert body == {"deletion_request_id": request_id, "status": "completed"}


def test_unknown_deletion_request_is_404(client: TestClient) -> None:
    """An unguessable id that does not exist discloses nothing."""
    assert client.get("/v1/privacy/delete/no-such-request").status_code == 404


def test_deletion_writes_a_surviving_audit_event(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """The trail must outlive the erasure it records.

    "I never asked for a deletion" is the repudiation claim audit exists to
    answer, so the event cannot be cascaded away with the tenant.
    """
    csrf = _login(client, factory)
    request_id = client.post("/v1/privacy/delete", headers={CSRF_HEADER_NAME: csrf}).json()[
        "deletion_request_id"
    ]

    with factory() as session:
        assert session.get(Tenant, "tenant-1") is None
        event = session.scalars(select(AuditEventRow)).one()

    assert event.action == "delete"
    assert event.tenant_id == "tenant-1"
    assert event.resource_id == request_id
    assert event.actor_type == "user"
    # Counts and health values stay out of the trail.
    assert json.loads(event.metadata_json) == {"outcome": "completed"}
    assert AuditRepository(factory).verify() is None


def test_audit_metadata_carries_no_health_values(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """An audit trail that logged measurements would be a second PHI store."""
    _add_health_row(factory, tenant_id="tenant-1", row_id="ci-1")
    csrf = _login(client, factory)
    client.post("/v1/privacy/delete", headers={CSRF_HEADER_NAME: csrf})

    with factory() as session:
        event = session.scalars(select(AuditEventRow)).one()

    blob = event.metadata_json.lower()
    for token in ("hrv", "sleep", "score", "steps", "energy", "stress"):
        assert token not in blob
