"""The migration seeds a reserved system tenant that can own system jobs."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Job, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.tenants import SYSTEM_TENANT_ID

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "system_tenant.db"
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
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
    session_factory = create_session_factory(engine)
    try:
        yield session_factory
    finally:
        engine.dispose()


def test_system_tenant_is_seeded(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        row = session.get(Tenant, SYSTEM_TENANT_ID)
    assert row is not None
    assert row.status == "active"
    assert row.display_name == "System"


def test_only_one_system_tenant(factory: sessionmaker[Session]) -> None:
    # The insert is idempotent (INSERT OR IGNORE); a single upgrade yields one.
    with factory() as session:
        ids = session.scalars(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID)).all()
    assert len(ids) == 1


def test_system_tenant_can_own_a_job(factory: sessionmaker[Session]) -> None:
    # The whole point: a system-wide job's tenant_id FK resolves to this tenant.
    enqueued = JobRepository(factory).enqueue_job(
        job_id="sys-1",
        tenant_id=SYSTEM_TENANT_ID,
        job_type="connection.reconcile_sweep",
        payload_json="{}",
        now=T0,
        idempotency_key="reconcile_sweep",
    )
    assert enqueued.created is True
    with factory() as session:
        job = session.get(Job, "sys-1")
    assert job is not None
    assert job.tenant_id == SYSTEM_TENANT_ID


def test_system_tenant_id_is_the_reserved_constant() -> None:
    assert SYSTEM_TENANT_ID == "system"
    # A fixed, non-UUID id so it is visibly a system row.
    assert "-" not in SYSTEM_TENANT_ID
