"""Tests for the webhook inbox repository (dedupe insert, status advance)."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant
from akunaki.adapters.db.webhook_inbox_repository import WebhookInboxRepository
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
KEK = b"\x55" * KEY_BYTES


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "inbox.db"
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
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
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
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(session_factory).link(
        connection_id="conn-1",
        tenant_id="tenant-1",
        provider=Provider.POLAR,
        sealed_secret=sealer.seal(b'{"access_token":"at"}', aad=b"conn-1"),
        scopes=("accesslink.read_all",),
        external_user_id=None,
        now=T0,
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _record(repo: WebhookInboxRepository, *, inbox_id: str, dedupe_key: str) -> object:
    return repo.record_delivery(
        inbox_id=inbox_id,
        tenant_id="tenant-1",
        connection_id="conn-1",
        provider="polar",
        dedupe_key=dedupe_key,
        delivery_id=dedupe_key,
        headers_meta={"content_type": "application/json"},
        now=T0,
    )


def test_first_delivery_is_recorded(factory: sessionmaker[Session]) -> None:
    repo = WebhookInboxRepository(factory)
    result = _record(repo, inbox_id="in-1", dedupe_key="d-1")
    assert result.is_duplicate is False  # type: ignore[attr-defined]
    assert result.inbox_id == "in-1"  # type: ignore[attr-defined]


def test_duplicate_returns_the_original_row(factory: sessionmaker[Session]) -> None:
    repo = WebhookInboxRepository(factory)
    _record(repo, inbox_id="in-1", dedupe_key="d-1")
    dup = _record(repo, inbox_id="in-2", dedupe_key="d-1")
    assert dup.is_duplicate is True  # type: ignore[attr-defined]
    # Points at the first row's id, not the losing insert's.
    assert dup.inbox_id == "in-1"  # type: ignore[attr-defined]


def test_different_dedupe_keys_both_record(factory: sessionmaker[Session]) -> None:
    repo = WebhookInboxRepository(factory)
    a = _record(repo, inbox_id="in-1", dedupe_key="d-1")
    b = _record(repo, inbox_id="in-2", dedupe_key="d-2")
    assert a.is_duplicate is False  # type: ignore[attr-defined]
    assert b.is_duplicate is False  # type: ignore[attr-defined]


def test_empty_dedupe_key_is_rejected(factory: sessionmaker[Session]) -> None:
    repo = WebhookInboxRepository(factory)
    with pytest.raises(ValueError, match="dedupe_key must be non-empty"):
        _record(repo, inbox_id="in-1", dedupe_key="")


def test_mark_enqueued_advances_once(factory: sessionmaker[Session]) -> None:
    repo = WebhookInboxRepository(factory)
    _record(repo, inbox_id="in-1", dedupe_key="d-1")
    assert repo.mark_enqueued(inbox_id="in-1") is True
    # A second advance is a no-op: it is no longer in 'accepted'.
    assert repo.mark_enqueued(inbox_id="in-1") is False
