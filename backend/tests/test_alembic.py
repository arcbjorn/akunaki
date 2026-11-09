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
    cfg.set_main_option("script_location", str(backend_root / "src" / "akunaki" / "migrations"))
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


def test_0031_repairs_anomaly_intervals_that_ended_before_they_started(
    temp_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tracker advanced days out of order; 369 production rows ended before they began.

    The migration deletes those — an interval that ends before it starts
    describes nothing — and stamps every survivor with the last day it records.
    """
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", temp_db_url)
    clear_settings_cache()
    cfg = _alembic_config(temp_db_url)
    command.upgrade(cfg, "20260906_0030")

    engine = create_engine(temp_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tenants (id, created_at, status, primary_timezone, display_name)
                    VALUES ('t1', '2026-11-08T00:00:00Z', 'active', 'UTC', 'T')
                    """
                )
            )
            rows = [
                ("garbage", "2026-10-05", "2026-09-29", 0),
                ("closed", "2026-09-28", "2026-09-30", 0),
                ("open", "2026-10-01", None, 1),
            ]
            for anomaly_id, started_on, ended_on, is_active in rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO anomalies (
                            id, tenant_id, feature_code, started_on, ended_on, severity,
                            z_like, formula_version, is_active, consecutive_clear_days,
                            created_at, updated_at
                        ) VALUES (
                            :id, 't1', 'elevated_rhr', :started_on, :ended_on, 'moderate',
                            2.7, 'anomaly_v0.1.0', :is_active, 0,
                            '2026-11-08T00:00:00Z', '2026-11-08T00:00:00Z'
                        )
                        """
                    ),
                    {
                        "id": anomaly_id,
                        "started_on": started_on,
                        "ended_on": ended_on,
                        "is_active": is_active,
                    },
                )

        command.upgrade(cfg, "head")

        with engine.begin() as conn:
            survivors = conn.execute(
                text("SELECT id, tracked_through FROM anomalies ORDER BY id")
            ).all()
        assert [tuple(row) for row in survivors] == [
            ("closed", "2026-09-30"),
            ("open", "2026-10-01"),
        ]
    finally:
        engine.dispose()
        clear_settings_cache()


def test_0032_relabels_google_relayed_sleep_facts(
    temp_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every sleep fact was stamped ``oura``; the ones Google Health relayed were not."""
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", temp_db_url)
    clear_settings_cache()
    cfg = _alembic_config(temp_db_url)
    command.upgrade(cfg, "20261108_0031")

    engine = create_engine(temp_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tenants (id, created_at, status, primary_timezone, display_name)
                    VALUES ('t1', '2026-11-09T00:00:00Z', 'active', 'UTC', 'T')
                    """
                )
            )
            for fact_id, entity_type, schema_version in (
                ("band", "sleep_session", "google_health.v4"),
                ("ring", "sleep_session", "oura.v2"),
                ("hrv", "overnight_vitals", "oura.v2"),
            ):
                conn.execute(
                    text(
                        """
                        INSERT INTO fact_records (
                            id, tenant_id, provider, entity_type, method, quality,
                            confidence, schema_version, normalizer_version, fact_key,
                            version_n, is_current, deletion_state, exclude_from_load,
                            created_at
                        ) VALUES (
                            :id, 't1', 'oura', :entity_type, 'wearable', 'high',
                            1.0, :schema_version, 'n1', :id,
                            1, 1, 'active', 0,
                            '2026-11-09T00:00:00Z'
                        )
                        """
                    ),
                    {"id": fact_id, "entity_type": entity_type, "schema_version": schema_version},
                )

        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            rows = conn.execute(text("SELECT id, provider FROM fact_records ORDER BY id")).all()
        assert [tuple(row) for row in rows] == [
            ("band", "google_health"),
            ("hrv", "oura"),
            ("ring", "oura"),
        ]

        command.downgrade(cfg, "20261108_0031")
        with engine.begin() as conn:
            band = conn.execute(text("SELECT provider FROM fact_records WHERE id = 'band'")).one()
        assert tuple(band) == ("oura",)
    finally:
        engine.dispose()
        clear_settings_cache()
