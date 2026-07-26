"""Run a real Oura sync end to end against the linked connection (local dev).

Drives the production worker path — the same `build_registry` wiring the real
worker entrypoint uses — so this exercises fetch -> ingest -> normalize ->
score, not a stub. Jobs are enqueued and drained through the real `JobWorker`,
so retries, leases, and dead-lettering behave exactly as in production.

Usage:

    uv run python scripts/oura_sync.py            # sync + normalize + score
    uv run python scripts/oura_sync.py --report   # also print what landed
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import (
    Connection,
    FactRecord,
    OvernightVitals,
    RawRevision,
    SleepSession,
)
from akunaki.adapters.wiring.registry import build_registry
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import INITIAL_SYNC_JOB_TYPE

PROVIDER = "oura"
MAX_DRAIN = 200


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="print what landed")
    args = parser.parse_args()

    clear_settings_cache()
    settings = Settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)

    with factory() as session:
        connection = session.scalars(
            select(Connection).where(Connection.provider == PROVIDER)
        ).first()
        if connection is None:
            raise SystemExit("no Oura connection linked — run scripts/oura_link.py link first")
        connection_id = connection.id
        tenant_id = connection.tenant_id

    now = datetime.now(UTC)
    jobs = JobRepository(factory)
    jobs.enqueue_job(
        job_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        job_type=INITIAL_SYNC_JOB_TYPE,
        payload_json=f'{{"connection_id":"{connection_id}"}}',
        now=now,
        max_attempts=3,
    )

    worker = JobWorker(
        jobs,
        owner="local-sync",
        config=WorkerConfig(lease_ttl=timedelta(seconds=120)),
        registry=build_registry(settings, factory),
        jitter=lambda: 0.0,
    )

    # Sync enqueues normalize, which enqueues score.recompute; drain until idle
    # so the whole chain runs in one pass.
    drained = 0
    while drained < MAX_DRAIN and worker.run_once():
        drained += 1

    stats = worker.stats
    print(
        f"\njobs run: {drained} | succeeded={stats.succeeded} "
        f"retried={stats.retried} dead_lettered={stats.dead_lettered}"
    )

    if args.report:
        _report(factory)


def _report(factory: object) -> None:
    # The day/quality live on FactRecord; the detail tables hold the values.
    with factory() as session:  # type: ignore[operator]
        revisions = session.scalar(select(func.count()).select_from(RawRevision)) or 0
        sleep_rows = session.execute(
            select(FactRecord, SleepSession)
            .join(SleepSession, SleepSession.fact_record_id == FactRecord.id)
            .where(FactRecord.is_current.is_(True))
        ).all()
        vitals_rows = session.execute(
            select(FactRecord, OvernightVitals)
            .join(OvernightVitals, OvernightVitals.fact_record_id == FactRecord.id)
            .where(FactRecord.is_current.is_(True))
        ).all()

    naps = sum(1 for _fact, sleep in sleep_rows if sleep.is_nap)
    non_nap = len(sleep_rows) - naps

    print(f"raw revisions   : {revisions}")
    print(f"sleep sessions  : {len(sleep_rows)}  (naps: {naps}, main: {non_nap})")
    print(f"overnight vitals: {len(vitals_rows)}")
    if len(vitals_rows) > non_nap:
        print("  WARNING: more vitals than main sleeps — the nap guard has regressed.")
    else:
        print("  ok: vitals never exceed main sleeps, so no nap leaked in.")

    for fact, vitals in sorted(vitals_rows, key=lambda r: r[0].local_health_day)[:7]:
        print(
            f"  {fact.local_health_day}  hrv={vitals.hrv_ms}  rhr={vitals.resting_hr_bpm}  "
            f"temp={vitals.temperature_deviation_c}  resp={vitals.respiratory_rate_bpm}"
        )


if __name__ == "__main__":
    main()
