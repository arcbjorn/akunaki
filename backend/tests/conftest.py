"""Shared fixtures: temp libSQL files only; no leftover DB artifacts."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
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


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep the suite independent of the developer's local environment.

    ``Settings`` reads ``backend/.env`` and every ``AKUNAKI_*`` variable, so a
    real local config (connector credentials, a KEK, a database URL) would leak
    into tests that assert defaults — passing or failing depending on whose
    machine ran them. Anchoring the env-file at an empty temp path and clearing
    the prefix makes each test start from documented defaults; tests that need
    a value still set it explicitly via monkeypatch.
    """
    for key in list(os.environ):
        if key.startswith("AKUNAKI_"):
            monkeypatch.delenv(key, raising=False)
    # Point pydantic-settings at a path that does not exist, so no .env is read.
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    yield
    clear_settings_cache()


def head_revision() -> str:
    """Current head revision, derived from the migration scripts.

    Asserting a literal head id would break on every new migration; callers
    verify that ``upgrade head`` lands on head, not which id that is.
    """
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_backend_root() / "src" / "akunaki" / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head() or ""


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(_backend_root() / "src" / "akunaki" / "migrations"))
    return cfg


# Migrating a fresh database costs ~0.2s of alembic per test; across the suite
# that is minutes of pure setup. The chain runs ONCE per process into this
# template file, and every later request is a file copy (~1ms). The template
# is never opened after it is built, so copies are always of a quiescent file.
_template_dir: str | None = None


def _template_db() -> Path:
    global _template_dir
    if _template_dir is None:
        tmp = tempfile.mkdtemp(prefix="akunaki-migrated-template-")
        path = Path(tmp) / "template.db"
        # env.py resolves the URL from Settings, never from alembic.ini, so
        # the template build must speak through the environment and restore
        # whatever the calling test had set.
        previous = os.environ.get("AKUNAKI_DATABASE_URL")
        os.environ["AKUNAKI_DATABASE_URL"] = f"sqlite+libsql:///{path}"
        clear_settings_cache()
        try:
            command.upgrade(_alembic_config(f"sqlite+libsql:///{path}"), "head")
        finally:
            if previous is None:
                os.environ.pop("AKUNAKI_DATABASE_URL", None)
            else:
                os.environ["AKUNAKI_DATABASE_URL"] = previous
            clear_settings_cache()
        atexit.register(shutil.rmtree, tmp, ignore_errors=True)
        _template_dir = tmp
    return Path(_template_dir) / "template.db"


def upgrade_to_head(database_url: str) -> None:
    """Bring the file database at ``database_url`` to the migration head.

    Copies a once-migrated template for the common fresh-file case; anything
    else (a memory URL, a file that already exists) runs the real chain, so
    behaviour is identical and only the cost changes.
    """
    prefix = "sqlite+libsql:///"
    target = Path(database_url[len(prefix) :]) if database_url.startswith(prefix) else None
    if target is None or target.exists() or not target.parent.is_dir():
        command.upgrade(_alembic_config(database_url), "head")
        return
    template = _template_db()
    shutil.copyfile(template, target)
    for suffix in ("-wal", "-shm"):
        sibling = template.with_name(template.name + suffix)
        if sibling.exists():
            shutil.copyfile(sibling, Path(str(target) + suffix))


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
    upgrade_to_head(settings.database_url)
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
    upgrade_to_head(settings.database_url)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    clear_settings_cache()
