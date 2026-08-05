"""``SyncRunRepository``: durable history of sync attempts.

``sync_runs`` shipped with the transport migration and had no writer, so
``raw_payloads.sync_run_id`` and ``raw_revisions.sync_run_id`` were permanently
NULL. These pin the lifecycle: opened before the fetch, closed once, never
rewritten.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Connection, Tenant
from akunaki.adapters.db.sync_run_repository import SyncRunRepository
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.sync_runs import SyncRunStatus, SyncRunTrigger

T0 = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "sync_runs.db"
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
    with session_factory() as session, session.begin():
        for suffix in ("1", "2"):
            session.add(
                Tenant(
                    id=f"tenant-{suffix}",
                    created_at=NOW_S,
                    status="active",
                    primary_timezone="UTC",
                    display_name=None,
                )
            )
            session.add(
                Connection(
                    id=f"conn-{suffix}",
                    tenant_id=f"tenant-{suffix}",
                    provider="oura" if suffix == "1" else "polar",
                    status="active",
                    scopes_granted_json="[]",
                    external_user_id=None,
                    connected_at=NOW_S,
                    updated_at=NOW_S,
                )
            )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _open(
    repo: SyncRunRepository,
    *,
    run_id: str = "run-1",
    tenant_id: str = "tenant-1",
    connection_id: str = "conn-1",
    trigger: SyncRunTrigger = SyncRunTrigger.SCHEDULE,
    stream: str | None = "sleep",
    now: datetime = T0,
) -> str:
    return repo.open(
        run_id=run_id,
        tenant_id=tenant_id,
        connection_id=connection_id,
        trigger=trigger,
        stream=stream,
        now=now,
    )


def test_an_open_run_is_visible_before_it_finishes(factory: sessionmaker[Session]) -> None:
    """The reason runs are opened up front, not written on completion.

    An attempt that dies mid-flight must leave a trace; a row written only at
    the end would lose exactly the failures worth seeing.
    """
    repo = SyncRunRepository(factory)
    _open(repo)

    [run] = repo.recent_for_tenant(tenant_id="tenant-1", limit=10)

    assert run.status == "running"
    assert run.finished_at is None
    assert run.error_class is None


def test_closing_settles_the_outcome(factory: sessionmaker[Session]) -> None:
    repo = SyncRunRepository(factory)
    _open(repo)

    assert repo.close(run_id="run-1", status=SyncRunStatus.SUCCEEDED, now=T0) is True

    [run] = repo.recent_for_tenant(tenant_id="tenant-1", limit=10)
    assert run.status == "succeeded"
    assert run.finished_at is not None


def test_a_failure_records_its_error_class(factory: sessionmaker[Session]) -> None:
    repo = SyncRunRepository(factory)
    _open(repo)

    repo.close(
        run_id="run-1",
        status=SyncRunStatus.FAILED,
        now=T0,
        error_class="TransientJobError",
    )

    [run] = repo.recent_for_tenant(tenant_id="tenant-1", limit=10)
    assert run.status == "failed"
    assert run.error_class == "TransientJobError"


def test_a_settled_run_is_never_rewritten(factory: sessionmaker[Session]) -> None:
    """A retry reusing an id must not turn a recorded failure into a success."""
    repo = SyncRunRepository(factory)
    _open(repo)
    repo.close(run_id="run-1", status=SyncRunStatus.FAILED, now=T0, error_class="boom")

    reclosed = repo.close(run_id="run-1", status=SyncRunStatus.SUCCEEDED, now=T0)

    assert reclosed is False
    [run] = repo.recent_for_tenant(tenant_id="tenant-1", limit=10)
    assert run.status == "failed"
    assert run.error_class == "boom"


def test_closing_an_unknown_run_reports_no_write(factory: sessionmaker[Session]) -> None:
    repo = SyncRunRepository(factory)

    assert repo.close(run_id="ghost", status=SyncRunStatus.SUCCEEDED, now=T0) is False


def test_close_refuses_a_non_terminal_status(factory: sessionmaker[Session]) -> None:
    """``running`` is an opening state; closing into it would erase the outcome."""
    repo = SyncRunRepository(factory)
    _open(repo)

    with pytest.raises(ValueError, match="terminal status"):
        repo.close(run_id="run-1", status=SyncRunStatus.RUNNING, now=T0)


def test_runs_are_newest_first(factory: sessionmaker[Session]) -> None:
    repo = SyncRunRepository(factory)
    _open(repo, run_id="older", now=T0)
    _open(repo, run_id="newer", now=T0 + timedelta(minutes=5))

    runs = repo.recent_for_tenant(tenant_id="tenant-1", limit=10)

    assert [run.run_id for run in runs] == ["newer", "older"]


def test_runs_sharing_an_instant_have_a_stable_order(factory: sessionmaker[Session]) -> None:
    """Without the id tie-break, two runs in one second could swap between polls."""
    repo = SyncRunRepository(factory)
    _open(repo, run_id="run-a", now=T0)
    _open(repo, run_id="run-b", now=T0)

    first = [run.run_id for run in repo.recent_for_tenant(tenant_id="tenant-1", limit=10)]
    second = [run.run_id for run in repo.recent_for_tenant(tenant_id="tenant-1", limit=10)]

    assert first == second == ["run-b", "run-a"]


def test_the_limit_is_honored(factory: sessionmaker[Session]) -> None:
    repo = SyncRunRepository(factory)
    for index in range(5):
        _open(repo, run_id=f"run-{index}", now=T0 + timedelta(minutes=index))

    assert len(repo.recent_for_tenant(tenant_id="tenant-1", limit=2)) == 2


def test_never_reads_another_tenants_runs(factory: sessionmaker[Session]) -> None:
    repo = SyncRunRepository(factory)
    _open(repo, run_id="theirs", tenant_id="tenant-2", connection_id="conn-2")

    assert repo.recent_for_tenant(tenant_id="tenant-1", limit=10) == []


def test_a_run_carries_its_provider(factory: sessionmaker[Session]) -> None:
    """Joined from the connection: a run id alone means nothing to a user."""
    repo = SyncRunRepository(factory)
    _open(repo, run_id="theirs", tenant_id="tenant-2", connection_id="conn-2")

    [run] = repo.recent_for_tenant(tenant_id="tenant-2", limit=10)

    assert run.provider == "polar"
    assert run.trigger == "schedule"
    assert run.stream == "sleep"
