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
from types import FrameType

from akunaki.adapters.db.engine import (
    create_db_engine,
    create_session_factory,
    probe_database_ready,
)
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.application.worker_runtime import JobWorker, WorkerConfig
from akunaki.config import get_settings

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

        repository = JobRepository(create_session_factory(engine))
        worker = JobWorker(
            repository,
            owner=build_owner(),
            config=WorkerConfig(),
            stop_event=event,
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
