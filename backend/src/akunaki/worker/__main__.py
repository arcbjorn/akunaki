"""Entrypoint: python -m akunaki.worker

Boots core config and DB, probes readiness, then runs the durable job claim
loop until SIGINT/SIGTERM requests a cooperative shutdown.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import uuid
from datetime import timedelta
from types import FrameType

from akunaki.adapters.db.engine import (
    create_db_engine,
    create_session_factory,
    probe_database_ready,
)
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.wiring.registry import build_registry
from akunaki.application.audit_handlers import AUDIT_VERIFY_JOB_TYPE
from akunaki.application.retention_handlers import RETENTION_SWEEP_JOB_TYPE
from akunaki.application.sync_handlers import RECONCILE_SWEEP_JOB_TYPE
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import get_settings
from akunaki.domain.schedule import ScheduleSpec
from akunaki.domain.tenants import SYSTEM_TENANT_ID

# How often the leader enqueues a reconciliation sweep. The sweep skips fresh
# connections, so a frequent tick is cheap; staleness is decided by the handler.
_RECONCILE_INTERVAL = timedelta(minutes=30)
# Hourly is enough for a control that detects *past* tampering: the evidence
# does not decay, and a tighter loop would rescan the whole chain for no gain.
_AUDIT_VERIFY_INTERVAL = timedelta(hours=1)
# Expired credential material is already unusable, so this is data minimization
# rather than correctness; hourly keeps the tables from growing without bound
# without hammering four DELETEs on a tight loop.
_RETENTION_INTERVAL = timedelta(hours=1)

logger = logging.getLogger("akunaki.worker")


def build_owner() -> str:
    """Return a unique lease owner identity for this process."""
    return f"core-worker-{uuid.uuid4()}"


def install_signal_handlers(stop_event: threading.Event) -> None:
    """Translate SIGINT/SIGTERM into a cooperative stop request."""

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("shutdown signal received", extra={"signal": signum})
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _request_stop)


def configure_logging() -> None:
    """Install process-wide structured logging.

    Called from :func:`main` only. ``run_worker`` deliberately does **not**
    configure logging: it is imported by tests and other callers, and a
    ``basicConfig`` there mutates the root logger for the whole process.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )


def run_worker(*, stop_event: threading.Event | None = None) -> int:
    """Boot core wiring and run the durable job claim loop.

    Returns the process exit code (0 on clean shutdown, 1 when the database is
    not ready at boot).
    """
    settings = get_settings()
    engine = create_db_engine(settings)
    try:
        if not probe_database_ready(engine):
            print("akunaki.worker: database not ready; aborting boot", file=sys.stderr)
            return 1

        event = stop_event if stop_event is not None else threading.Event()
        if stop_event is None:
            install_signal_handlers(event)

        session_factory = create_session_factory(engine)
        repository = JobRepository(session_factory)
        registry = build_registry(settings, session_factory)
        # The leader enqueues a reconciliation sweep on an interval, owned by the
        # reserved system tenant; the sweep fans out per-connection syncs.
        schedules = [
            ScheduleSpec(
                job_type=RECONCILE_SWEEP_JOB_TYPE,
                interval=_RECONCILE_INTERVAL,
                tenant_id=SYSTEM_TENANT_ID,
                idempotency_key="reconcile_sweep",
            ),
            # Verifies the audit chain and publishes the verdict as a gauge.
            # Leader-gated like the sweep, so one process checks however many
            # workers are running.
            ScheduleSpec(
                job_type=AUDIT_VERIFY_JOB_TYPE,
                interval=_AUDIT_VERIFY_INTERVAL,
                tenant_id=SYSTEM_TENANT_ID,
                idempotency_key="audit_verify_chain",
            ),
            # Deletes expired sessions, OAuth/login states, and confirmations.
            # Each row is already unusable past its expiry; sweeping stops the
            # stored secrets outliving the window they were issued for.
            ScheduleSpec(
                job_type=RETENTION_SWEEP_JOB_TYPE,
                interval=_RETENTION_INTERVAL,
                tenant_id=SYSTEM_TENANT_ID,
                idempotency_key="retention_sweep",
            ),
        ]
        worker = JobWorker(
            repository,
            owner=build_owner(),
            config=WorkerConfig(),
            registry=registry,
            stop_event=event,
            schedules=schedules,
        )
        stats = worker.run_forever()
        logger.info(
            "worker shutdown complete",
            extra={
                "claimed": stats.claimed,
                "succeeded": stats.succeeded,
                "retried": stats.retried,
                "dead_lettered": stats.dead_lettered,
            },
        )
        return 0
    finally:
        engine.dispose()


def main() -> None:
    """Process entry."""
    configure_logging()
    raise SystemExit(run_worker())


if __name__ == "__main__":
    main()
