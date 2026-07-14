"""Worker entrypoint boots core config/DB and exits without a job loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from akunaki.config import Settings, clear_settings_cache
from akunaki.worker.__main__ import run_worker_stub


def test_worker_stub_exits_cleanly(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from alembic import command
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    cfg.set_main_option("script_location", str(backend_root / "alembic"))

    monkeypatch.setenv("AKUNAKI_DATABASE_URL", settings.database_url)
    clear_settings_cache()
    command.upgrade(cfg, "head")

    code = run_worker_stub()
    assert code == 0
    out = capsys.readouterr().out
    assert "job claim loop not implemented yet" in out
    clear_settings_cache()
