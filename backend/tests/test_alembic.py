"""Alembic upgrade / downgrade / upgrade on a temp libSQL file."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from akunaki.config import clear_settings_cache
from conftest import head_revision


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
        assert "job_leases" in tables
        assert "leader_leases" in tables
        assert "job_attempts" in tables
        assert "job_dead_letters" in tables
        assert "alembic_version" in tables
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == head_revision()
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(temp_db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "tenants" not in tables
        assert "jobs" not in tables
        assert "job_leases" not in tables
        assert "leader_leases" not in tables
        assert "job_attempts" not in tables
        assert "job_dead_letters" not in tables
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(temp_db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "tenants" in tables
        assert "jobs" in tables
        assert "job_leases" in tables
        assert "leader_leases" in tables
        assert "job_attempts" in tables
        assert "job_dead_letters" in tables
        # DB file must live under this test's pytest tmp_path (no leftover outside temp).
        path_part = Path(temp_db_url.removeprefix("sqlite+libsql:///"))
        resolved_db = path_part.resolve()
        assert resolved_db.exists()
        assert resolved_db.is_relative_to(tmp_path.resolve())
    finally:
        engine.dispose()
        clear_settings_cache()


def test_head_to_0002_to_head_preserves_legacy_job(
    temp_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", temp_db_url)
    clear_settings_cache()
    cfg = _alembic_config(temp_db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260713_0002")

    engine = create_engine(temp_db_url)
    try:
        assert "job_type" not in {column["name"] for column in inspect(engine).get_columns("jobs")}
        with engine.begin() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == "20260713_0002"
            conn.execute(
                text(
                    """
                    INSERT INTO tenants (
                        id, created_at, status, primary_timezone, display_name
                    ) VALUES (
                        :id, :created_at, :status, :primary_timezone, :display_name
                    )
                    """
                ),
                {
                    "id": "legacy-tenant",
                    "created_at": "2026-07-13T12:00:00+00:00",
                    "status": "active",
                    "primary_timezone": "UTC",
                    "display_name": "Legacy Tenant",
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO jobs (
                        id, tenant_id, role, status, payload_json, priority,
                        run_after, attempts, max_attempts, idempotency_key,
                        fence_token, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :role, :status, :payload_json, :priority,
                        :run_after, :attempts, :max_attempts, :idempotency_key,
                        :fence_token, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "id": "legacy-job",
                    "tenant_id": "legacy-tenant",
                    "role": "core",
                    "status": "ready",
                    "payload_json": '{"legacy":true}',
                    "priority": 17,
                    "run_after": "2026-07-13T12:30:00+00:00",
                    "attempts": 2,
                    "max_attempts": 7,
                    "idempotency_key": "legacy-idempotency",
                    "fence_token": 4,
                    "created_at": "2026-07-13T12:00:00+00:00",
                    "updated_at": "2026-07-13T12:15:00+00:00",
                },
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(temp_db_url)
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            job = (
                conn.execute(
                    text(
                        """
                        SELECT
                            id, tenant_id, role, status, payload_json, priority,
                            run_after, attempts, max_attempts, idempotency_key,
                            fence_token, created_at, updated_at, job_type,
                            last_error_class
                        FROM jobs
                        WHERE id = :job_id
                        """
                    ),
                    {"job_id": "legacy-job"},
                )
                .mappings()
                .one()
            )

        assert version == head_revision()
        assert dict(job) == {
            "id": "legacy-job",
            "tenant_id": "legacy-tenant",
            "role": "core",
            "status": "ready",
            "payload_json": '{"legacy":true}',
            "priority": 17,
            "run_after": "2026-07-13T12:30:00+00:00",
            "attempts": 2,
            "max_attempts": 7,
            "idempotency_key": "legacy-idempotency",
            "fence_token": 4,
            "created_at": "2026-07-13T12:00:00+00:00",
            "updated_at": "2026-07-13T12:15:00+00:00",
            "job_type": "system.noop",
            "last_error_class": None,
        }
    finally:
        engine.dispose()
        clear_settings_cache()
