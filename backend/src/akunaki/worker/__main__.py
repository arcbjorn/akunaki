"""Entrypoint: python -m akunaki.worker

Boots core config and DB, probes readiness, then exits cleanly.
The durable job claim loop is not implemented yet (phase one).
"""

from __future__ import annotations

import sys

from akunaki.adapters.db.engine import create_db_engine, probe_database_ready
from akunaki.config import get_settings


def run_worker_stub() -> int:
    """Boot core wiring, check DB readiness, exit without a job loop.

    Returns process exit code (0 on successful stub run).
    """
    settings = get_settings()
    engine = create_db_engine(settings)
    try:
        ready = probe_database_ready(engine)
        if not ready:
            print("akunaki.worker: database not ready; aborting stub boot", file=sys.stderr)
            return 1
        print(
            "akunaki.worker: core config/DB ready; job claim loop not implemented yet (stub exit)"
        )
        return 0
    finally:
        engine.dispose()


def main() -> None:
    """Process entry."""
    raise SystemExit(run_worker_stub())


if __name__ == "__main__":
    main()
