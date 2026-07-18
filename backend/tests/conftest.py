"""Shared fixtures: temp libSQL files only; no leftover DB artifacts."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.api.app import create_app
from akunaki.config import Settings, clear_settings_cache


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def head_revision() -> str:
    """Current head revision, derived from the migration scripts.

    Asserting a literal head id would break on every new migration; callers
    verify that ``upgrade head`` lands on head, not which id that is.
    """
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head() or ""


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    return cfg


@pytest.fixture
def temp_db_url(tmp_path: Path) -> str:
    """sqlite+libsql URL pointing at a temp file (deleted with tmp_path)."""
    db_path = tmp_path / "test.db"
    return f"sqlite+libsql:///{db_path.resolve()}"


@pytest.fixture
def settings(temp_db_url: str, monkeypatch: pytest.MonkeyPatch) -> Generator[Settings]:
    clear_settings_cache()
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", temp_db_url)
    resolved = Settings(database_url=temp_db_url)
    yield resolved
    clear_settings_cache()


@pytest.fixture
def migrated_engine(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Generator[Engine]:
    """Engine against a temp DB with alembic upgrade head applied."""
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", settings.database_url)
    clear_settings_cache()
    cfg = _alembic_config(settings.database_url)
    command.upgrade(cfg, "head")
    engine = create_db_engine(settings)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_settings_cache()


@pytest.fixture
def session_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    """HTTP client against a migrated temp DB."""
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", settings.database_url)
    clear_settings_cache()
    cfg = _alembic_config(settings.database_url)
    command.upgrade(cfg, "head")
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    clear_settings_cache()
