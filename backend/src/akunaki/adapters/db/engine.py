"""SQLAlchemy 2 engine and session factory for local sqlite+libsql."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool

from akunaki.config import Settings

# Milliseconds. Short, bounded driver wait compatible with repository
# _run_short_tx retry budget (2.0 s). Kept low so contested transactions fail
# fast and the repository retries with a fresh Session.
BUSY_TIMEOUT_MS = 50


def _is_memory_libsql_url(database_url: str) -> bool:
    """Return True for supported in-memory sqlite+libsql URL forms.

    Both official empty (``sqlite+libsql://``) and path memory
    (``sqlite+libsql:///:memory:``) require a shared connection so schema and
    data survive across session checkouts on one Engine.
    """
    if not database_url.startswith("sqlite+libsql:"):
        return False
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite+libsql":
        return False
    path = parsed.path or ""
    if path == "":
        return True
    return path == "/:memory:" or path.endswith("/:memory:")


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

    Foreign keys and busy_timeout are enabled on every new DB-API connection.
    WAL is applied via the pool ``first_connect`` hook for file-backed URLs only
    (never for in-memory). In-memory URLs use StaticPool so separate sessions
    share one DB; file-backed engines use QueuePool with bounded checkout so
    concurrent short CAS transactions reuse physical DB-API connections without
    a process-global mutex or NullPool connection storms. Remote Turso
    credentials and connect_args are intentionally not wired.
    """
    _ensure_local_parent_dir(settings.database_url)
    memory = _is_memory_libsql_url(settings.database_url)

    if memory:
        # StaticPool: one shared connection for in-memory DBs (schema/data
        # survive session checkout).
        engine = create_engine(
            settings.database_url,
            echo=settings.echo_sql,
            poolclass=StaticPool,
            pool_pre_ping=False,
        )
    else:
        # QueuePool: bounded connection pool for file-backed DBs. Concurrent
        # short CAS transactions check out and return DB-API connections, giving
        # real connection reuse without NullPool's fresh-connection-per-checkout
        # overhead. pool_size + max_overflow cap concurrent physical connections;
        # pool_timeout bounds checkout wait.
        engine = create_engine(
            settings.database_url,
            echo=settings.echo_sql,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=5,
            pool_timeout=5,
            pool_pre_ping=False,
        )

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: Any, _connection_record: Any) -> None:
        """Enable FKs and busy_timeout on every new DB-API connection."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()

    if not memory:
        # WAL is a durable DB-level setting; apply once via first_connect only.
        # busy_timeout first so the journal_mode write can wait under contention.
        @event.listens_for(engine, "first_connect")
        def _configure_wal_once(dbapi_connection: Any, _connection_record: Any) -> None:
            """Set busy_timeout then WAL once for file-backed engines only."""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                cursor.execute("PRAGMA journal_mode=WAL")
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
