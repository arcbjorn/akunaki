"""Audit persistence against a migrated database.

The record has to survive the thing it describes: a privacy deletion erases the
tenant, and the proof that it happened must outlive it.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.audit_repository import AuditRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import AuditEventRow, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.audit import (
    GENESIS_HASH,
    ActorType,
    AuditAction,
    InvalidAuditMetadataError,
)
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def audit_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "audit.db"
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
def factory(audit_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=audit_db))
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


def _record(
    repo: AuditRepository,
    *,
    event_id: str,
    action: AuditAction = AuditAction.DELETE,
    tenant_id: str | None = "tenant-1",
    metadata: dict[str, str] | None = None,
    offset_seconds: int = 0,
) -> str:
    return repo.record(
        event_id=event_id,
        tenant_id=tenant_id,
        actor_type=ActorType.USER,
        actor_id="user-1",
        action=action,
        resource_type="tenant",
        resource_id="req-1",
        metadata=metadata if metadata is not None else {"outcome": "completed"},
        now=T0 + timedelta(seconds=offset_seconds),
    )


def test_first_event_links_to_genesis(factory: sessionmaker[Session]) -> None:
    repo = AuditRepository(factory)
    _record(repo, event_id="e1")

    with factory() as session:
        row = session.scalars(select(AuditEventRow)).one()
    assert row.previous_hash == GENESIS_HASH
    assert len(row.event_hash) == 64


def test_events_chain_in_insertion_order(factory: sessionmaker[Session]) -> None:
    repo = AuditRepository(factory)
    first = _record(repo, event_id="e1")
    _record(repo, event_id="e2", offset_seconds=1)

    with factory() as session:
        rows = session.scalars(select(AuditEventRow).order_by(AuditEventRow.seq)).all()
    assert rows[1].previous_hash == first
    assert repo.verify() is None


def test_editing_a_row_is_detected(factory: sessionmaker[Session]) -> None:
    """The point of the chain: a database edit does not go unnoticed."""
    repo = AuditRepository(factory)
    _record(repo, event_id="e1")
    _record(repo, event_id="e2", offset_seconds=1)
    assert repo.verify() is None

    with factory() as session, session.begin():
        session.execute(
            update(AuditEventRow)
            .where(AuditEventRow.id == "e1")
            .values(resource_id="something-else")
        )

    assert repo.verify() == 1


def test_deleting_a_row_is_detected(factory: sessionmaker[Session]) -> None:
    repo = AuditRepository(factory)
    _record(repo, event_id="e1")
    _record(repo, event_id="e2", offset_seconds=1)
    _record(repo, event_id="e3", offset_seconds=2)

    with factory() as session, session.begin():
        session.execute(update(AuditEventRow).where(AuditEventRow.id == "e2").values(seq=99))
    with factory() as session, session.begin():
        session.query(AuditEventRow).filter(AuditEventRow.id == "e2").delete()

    assert repo.verify() is not None


def test_health_metadata_is_refused_before_writing(
    factory: sessionmaker[Session],
) -> None:
    """A rejected record must leave no partial row behind."""
    repo = AuditRepository(factory)

    with pytest.raises(InvalidAuditMetadataError):
        _record(repo, event_id="e1", metadata={"hrv_ms": "62"})

    with factory() as session:
        assert session.scalars(select(AuditEventRow)).all() == []


def test_event_survives_the_tenant_it_describes(
    factory: sessionmaker[Session],
) -> None:
    """No FK to tenants: erasing a tenant must not erase the proof.

    A cascade here would mean a privacy deletion destroys its own audit trail —
    exactly the repudiation the trail exists to answer.
    """
    repo = AuditRepository(factory)
    _record(repo, event_id="e1")

    with factory() as session, session.begin():
        session.query(Tenant).filter(Tenant.id == "tenant-1").delete()

    with factory() as session:
        row = session.scalars(select(AuditEventRow)).one()
    assert row.tenant_id == "tenant-1"
    assert repo.verify() is None


def test_system_events_carry_no_tenant(factory: sessionmaker[Session]) -> None:
    """A scheduled action belongs to no customer."""
    repo = AuditRepository(factory)
    _record(repo, event_id="e1", tenant_id=None, action=AuditAction.CONNECTION_SYNC)

    with factory() as session:
        row = session.scalars(select(AuditEventRow)).one()
    assert row.tenant_id is None


def test_empty_chain_verifies(factory: sessionmaker[Session]) -> None:
    assert AuditRepository(factory).verify() is None
