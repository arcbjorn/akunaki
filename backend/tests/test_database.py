"""Database FK enforcement and basic CRUD on libSQL."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from akunaki.adapters.db.engine import create_db_engine, probe_database_ready
from akunaki.adapters.db.models import Job, Tenant
from akunaki.config import Settings

# sqlalchemy-libsql surfaces some SQLite constraint failures as ValueError.
ConstraintError = (IntegrityError, ValueError)


def _tenant(tenant_id: str = "tenant-1") -> Tenant:
    return Tenant(
        id=tenant_id,
        created_at="2026-07-13T00:00:00Z",
        status="active",
        primary_timezone="UTC",
        display_name="Test",
    )


def _job(
    *,
    job_id: str = "job-1",
    tenant_id: str = "tenant-1",
    payload: str = '{"kind":"ping"}',
    idempotency_key: str | None = "idem-1",
) -> Job:
    return Job(
        id=job_id,
        tenant_id=tenant_id,
        role="core",
        status="ready",
        payload_json=payload,
        priority=10,
        run_after="2026-07-13T00:00:00Z",
        attempts=0,
        max_attempts=5,
        idempotency_key=idempotency_key,
        fence_token=0,
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T00:00:00Z",
    )


def test_create_engine_makes_parent_directory(tmp_path: Path) -> None:
    """File-backed local URLs create missing parent directories safely."""
    nested = tmp_path / "a" / "b" / "c"
    db_path = nested / "akunaki.db"
    assert not nested.exists()
    url = f"sqlite+libsql:///{db_path.resolve()}"
    settings = Settings(database_url=url)
    engine = create_db_engine(settings)
    try:
        assert nested.is_dir()
        assert probe_database_ready(engine)
    finally:
        engine.dispose()


def test_tenant_and_job_crud(db_session: Session) -> None:
    db_session.add(_tenant())
    db_session.flush()
    db_session.add(_job())
    db_session.commit()

    tenants = db_session.scalars(select(Tenant)).all()
    jobs = db_session.scalars(select(Job)).all()
    assert len(tenants) == 1
    assert tenants[0].id == "tenant-1"
    assert len(jobs) == 1
    assert jobs[0].tenant_id == "tenant-1"
    assert jobs[0].role == "core"
    assert jobs[0].payload_json == '{"kind":"ping"}'


def test_foreign_key_enforced(db_session: Session) -> None:
    db_session.add(_job(tenant_id="missing-tenant"))
    with pytest.raises(ConstraintError, match=r"FOREIGN KEY|foreign key"):
        db_session.commit()
    db_session.rollback()


def test_json_valid_check(db_session: Session) -> None:
    db_session.add(_tenant())
    db_session.flush()
    db_session.add(_job(payload="not-json"))
    with pytest.raises(ConstraintError, match=r"CHECK constraint|json"):
        db_session.commit()
    db_session.rollback()


def test_idempotency_unique(db_session: Session) -> None:
    db_session.add(_tenant())
    db_session.flush()
    db_session.add(_job(job_id="j1", idempotency_key="same"))
    db_session.add(_job(job_id="j2", idempotency_key="same"))
    with pytest.raises(ConstraintError, match=r"UNIQUE constraint"):
        db_session.commit()
    db_session.rollback()
