"""Worker entrypoint boots core config/DB and runs the durable claim loop."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from akunaki.config import Settings, clear_settings_cache
from akunaki.worker.__main__ import build_owner, run_worker


def _migrate(settings: Settings) -> None:
    from alembic import command
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    command.upgrade(cfg, "head")


def test_worker_runs_and_shuts_down_cleanly(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", settings.database_url)
    clear_settings_cache()
    _migrate(settings)

    # Pre-set stop so the loop drains the (empty) queue and exits immediately,
    # exercising real boot, wiring, and shutdown without hanging the suite.
    stop = threading.Event()
    stop.set()

    assert run_worker(stop_event=stop) == 0
    clear_settings_cache()


def test_worker_owner_is_unique_per_process() -> None:
    # Lease ownership and fencing depend on distinct owner identities.
    assert build_owner() != build_owner()
    assert build_owner().startswith("core-worker-")
