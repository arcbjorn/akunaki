"""Worker entrypoint boots core config/DB and runs the durable claim loop."""

from __future__ import annotations

import threading

import pytest

from akunaki.config import Settings, clear_settings_cache
from akunaki.worker.__main__ import build_owner, run_worker
from conftest import upgrade_to_head


def _migrate(settings: Settings) -> None:

    upgrade_to_head(settings.database_url)


def test_worker_runs_and_shuts_down_cleanly(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", settings.database_url)
    # The worker builds the full product registry at boot, which constructs the
    # envelope sealer (sync opens sealed tokens), so a real worker needs a KEK.
    monkeypatch.setenv("AKUNAKI_SECRET_KEKS", "v1:QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
    monkeypatch.setenv("AKUNAKI_ACTIVE_KEK_VERSION", "v1")
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
