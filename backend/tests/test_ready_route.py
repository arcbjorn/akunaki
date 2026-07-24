"""End-to-end coverage of the /readyz readiness endpoint.

Verifies that a migrated DB reads ready with queue + leader detail, that a DB
behind the migration head reads not-ready (503), and that queue depth and
leader presence are reported from real rows.
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
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Tenant
from akunaki.api.app import create_app
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cfg(url: str) -> Config:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_backend_root() / "src" / "akunaki" / "migrations"))
    return cfg


@pytest.fixture
def route_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "ready.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    command.upgrade(_cfg(url), "head")
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
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def client(route_db: str) -> TestClient:
    return TestClient(create_app(Settings(database_url=route_db)))


def test_migrated_db_is_ready(client: TestClient, factory: sessionmaker[Session]) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["database_ready"] is True
    assert body["migration"]["at_head"] is True
    assert body["migration"]["db_revision"] == body["migration"]["code_head"]
    assert body["migration"]["code_head"]  # non-empty
    # No jobs enqueued yet.
    assert body["queue"] == {"ready": 0, "leased": 0, "dead_letter": 0}
    # No worker has acquired the reaper lease.
    assert body["leader_held"] is False
    assert resp.headers["Cache-Control"] == "no-store"


def test_queue_depth_reflects_ready_jobs(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    repo = JobRepository(factory)
    repo.enqueue_job(
        job_id="j1",
        tenant_id="tenant-1",
        job_type="system.noop",
        payload_json="{}",
        now=T0,
    )
    repo.enqueue_job(
        job_id="j2",
        tenant_id="tenant-1",
        job_type="system.noop",
        payload_json="{}",
        now=T0,
    )
    body = client.get("/readyz").json()
    assert body["queue"]["ready"] == 2


def test_leader_held_when_lease_acquired(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    JobRepository(factory).try_acquire_leader(
        lease_name="core-reaper",
        owner="worker-1",
        lease_ttl=timedelta(seconds=30),
        now=T0,
    )
    body = client.get("/readyz").json()
    assert body["leader_held"] is True


def test_db_behind_head_is_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Migrate only partway: the DB is reachable but not at the code head, so the
    # deployment is not ready (a 503 for the readiness probe).
    db_path = tmp_path / "behind.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    command.upgrade(_cfg(url), "20260713_0002")  # an early revision, not head

    client = TestClient(create_app(Settings(database_url=url)))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["database_ready"] is True  # DB is reachable...
    assert body["migration"]["at_head"] is False  # ...but behind head
    clear_settings_cache()


def test_migrations_are_packaged_not_path_derived() -> None:
    """Readiness must resolve the migration head without a source checkout.

    The scripts live inside the package and are located by import, so a wheel
    carries them. Deriving the location by walking parent directories, or
    requiring ``alembic.ini`` on disk, would make ``/readyz`` raise on any
    installed deployment instead of reporting readiness.
    """
    from alembic.script import ScriptDirectory

    import akunaki
    from akunaki.api.routes.ready import _alembic_config
    from akunaki.migrations import script_location

    package_root = Path(akunaki.__file__).resolve().parent
    location = script_location()

    # Inside the installed package, so a wheel carries them.
    assert location.is_relative_to(package_root)
    assert (location / "versions").is_dir()
    assert any((location / "versions").glob("*.py"))

    # The head is readable from the config the endpoint itself builds, with no
    # alembic.ini on disk — the file is not shipped and must not be required.
    cfg = _alembic_config()
    assert cfg.config_file_name is None
    assert ScriptDirectory.from_config(cfg).get_current_head() is not None
