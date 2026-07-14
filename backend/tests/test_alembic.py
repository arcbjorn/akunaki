"""Alembic upgrade / downgrade / upgrade on a temp libSQL file."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from akunaki.config import clear_settings_cache


def _alembic_config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    return cfg


def test_upgrade_downgrade_upgrade(
    temp_db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", temp_db_url)
    clear_settings_cache()
    cfg = _alembic_config(temp_db_url)

    command.upgrade(cfg, "head")
    engine = create_engine(temp_db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "tenants" in tables
        assert "jobs" in tables
        assert "alembic_version" in tables
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "20260713_0001"
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(temp_db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "tenants" not in tables
        assert "jobs" not in tables
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(temp_db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "tenants" in tables
        assert "jobs" in tables
        # DB file must live under this test's pytest tmp_path (no leftover outside temp).
        path_part = Path(temp_db_url.removeprefix("sqlite+libsql:///"))
        resolved_db = path_part.resolve()
        assert resolved_db.exists()
        assert resolved_db.is_relative_to(tmp_path.resolve())
    finally:
        engine.dispose()
        clear_settings_cache()
