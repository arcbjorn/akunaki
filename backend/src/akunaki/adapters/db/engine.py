"""SQLAlchemy 2 engine and session factory for local sqlite+libsql."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from akunaki.config import Settings


def _ensure_local_parent_dir(database_url: str) -> None:
    """Create parent directory for local file-backed libSQL URLs when needed."""
    # sqlite+libsql:////abs/path or sqlite+libsql:///rel/path
    if not database_url.startswith("sqlite+libsql:"):
        return
    # Parse after the scheme; treat three-slash local file forms.
    rest = database_url.removeprefix("sqlite+libsql:")
    if rest.startswith("////"):
        # Absolute path: sqlite+libsql:////abs/path
        path_str = unquote(rest[3:])  # keep leading /
    elif rest.startswith("///"):
        # Relative or absolute with three slashes: sqlite+libsql:///path
        path_str = unquote(rest[3:])
        if path_str.startswith(":memory:"):
            return
        if not path_str.startswith("/"):
            path_str = str(Path.cwd() / path_str)
    else:
        return
    if not path_str or path_str == ":memory:":
        return
    parent = Path(path_str).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(settings: Settings) -> Engine:
    """Build a SQLAlchemy engine using the official local sqlite+libsql dialect.

    Foreign keys are enabled on every new DB-API connection.
    Remote Turso credentials and connect_args are intentionally not wired.
    """
    _ensure_local_parent_dir(settings.database_url)

    engine = create_engine(
        settings.database_url,
        echo=settings.echo_sql,
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Generator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def probe_database_ready(engine: Engine) -> bool:
    """Return True when a simple connectivity probe succeeds."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
