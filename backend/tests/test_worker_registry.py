"""The production handler registry wires every product job type.

Builds the real registry against a migrated DB and confirms every job type
resolves to a handler, then drives a reconcile-sweep job through the real worker
to prove the schedule → sweep → incremental-sync chain has handlers end to end
(no dead-lettering for a missing handler).
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import Job, Tenant
from akunaki.adapters.wiring.registry import build_registry, sync_configs
from akunaki.application.score_handlers import SCORE_RECOMPUTE_JOB_TYPE
from akunaki.application.sync_handlers import (
    DEFAULT_LOOKBACK_DAYS,
    INCREMENTAL_SYNC_JOB_TYPE,
    INITIAL_SYNC_JOB_TYPE,
    NORMALIZE_JOB_TYPE,
    RECONCILE_SWEEP_JOB_TYPE,
    streams_for_provider,
)
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.connections import Provider
from akunaki.domain.tenants import SYSTEM_TENANT_ID
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
KEK = b"\x55" * KEY_BYTES
KEK_B64 = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="  # 32 bytes


def _settings(url: str) -> Settings:
    return Settings(database_url=url, secret_keks=f"v1:{KEK_B64}", active_kek_version="v1")


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "registry.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(_settings(db_url))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at="2026-07-24T00:00:00Z",
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


def test_registry_has_every_product_job_type(db_url: str, factory: sessionmaker[Session]) -> None:
    registry = build_registry(_settings(db_url), factory)
    for job_type in (
        INITIAL_SYNC_JOB_TYPE,
        INCREMENTAL_SYNC_JOB_TYPE,
        NORMALIZE_JOB_TYPE,
        SCORE_RECOMPUTE_JOB_TYPE,
        RECONCILE_SWEEP_JOB_TYPE,
    ):
        assert registry.get(job_type) is not None


def test_the_configured_lookback_reaches_every_stream(db_url: str) -> None:
    """``AKUNAKI_LOOKBACK_DAYS`` is what a deployment actually backfills with.

    The window used to be a module constant no configuration could reach, so a
    deployment could never backfill more than 30 days. The setting has to land
    on **every** ``(provider, stream)`` the worker syncs, not just the first: a
    lookback that applied to one stream and not its siblings would be worse
    than none, because the shortfall would be invisible.
    """
    settings = Settings(
        database_url=db_url,
        secret_keks=f"v1:{KEK_B64}",
        active_kek_version="v1",
        lookback_days=365,
    )

    configs = sync_configs(settings)

    assert configs, "the worker must sync at least one stream"
    assert {config.lookback_days for config in configs.values()} == {365}
    # Every stream the dispatcher can route to has a config, so none silently
    # falls back to the module default.
    for provider in ("oura", "polar", "google_health"):
        for stream, _schema in streams_for_provider(provider):
            assert (provider, stream) in configs


def test_the_lookback_default_is_thirty_days(db_url: str) -> None:
    """Unset, the deployment keeps the settled 30-day window."""
    configs = sync_configs(_settings(db_url))

    assert {config.lookback_days for config in configs.values()} == {DEFAULT_LOOKBACK_DAYS}


def test_the_lookback_setting_refuses_a_meaningless_window(db_url: str) -> None:
    """A zero or negative window would backfill nothing; reject it at load.

    ``SyncConfig`` already refuses it, but that failure would surface inside a
    worker at wiring time. Validating at settings load stops the process with a
    message naming the variable instead.
    """
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            Settings(database_url=db_url, lookback_days=invalid)


def test_reconcile_sweep_enqueues_a_syncable_job(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    # A stale Oura connection exists; the reconcile sweep should enqueue an
    # incremental sync for it, and that job type must have a handler (not
    # dead-letter). We run the sweep job through the real worker.
    sealer = EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")
    ConnectionRepository(factory).link(
        connection_id="conn-oura",
        tenant_id="tenant-1",
        provider=Provider.OURA,
        sealed_secret=sealer.seal(b'{"access_token":"at"}', aad=b"conn-oura"),
        scopes=("daily",),
        external_user_id=None,
        now=T0 - timedelta(days=1),  # linked long ago -> stale
    )

    registry = build_registry(_settings(db_url), factory)
    repository = JobRepository(factory)
    repository.enqueue_job(
        job_id="sweep-1",
        tenant_id=SYSTEM_TENANT_ID,
        job_type=RECONCILE_SWEEP_JOB_TYPE,
        payload_json="{}",
        now=T0,
    )
    worker = JobWorker(
        repository,
        owner="worker-1",
        config=WorkerConfig(),
        registry=registry,
        clock=lambda: T0,
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    # Run the sweep; it enqueues an incremental sync for the stale connection.
    worker.run_once()

    with factory() as session:
        incremental = session.scalars(
            select(Job).where(Job.job_type == INCREMENTAL_SYNC_JOB_TYPE)
        ).all()
        sweep = session.get(Job, "sweep-1")
    # One job per stream the provider serves, so each stream keeps its own
    # cursor and retry budget.
    expected_streams = {stream for stream, _ in streams_for_provider("oura")}
    assert len(incremental) == len(expected_streams)
    assert {json.loads(j.payload_json)["stream"] for j in incremental} == expected_streams
    assert {j.tenant_id for j in incremental} == {"tenant-1"}
    # The sweep job itself completed (it did not dead-letter for a missing handler).
    assert sweep is not None
    assert sweep.status == "succeeded"
