"""Migration DDL and ORM models agree on the foundation schema."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import Table

from akunaki.adapters.db.models import Job, JobLease, LeaderLease, Tenant


def test_tables_match_models(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert set(insp.get_table_names()) >= {
        "tenants",
        "jobs",
        "job_leases",
        "leader_leases",
        "alembic_version",
    }

    tenant_cols = {c["name"] for c in insp.get_columns("tenants")}
    job_cols = {c["name"] for c in insp.get_columns("jobs")}
    lease_cols = {c["name"] for c in insp.get_columns("job_leases")}
    leader_cols = {c["name"] for c in insp.get_columns("leader_leases")}

    assert tenant_cols == {c.name for c in Tenant.__table__.columns}
    assert job_cols == {c.name for c in Job.__table__.columns}
    assert lease_cols == {c.name for c in JobLease.__table__.columns}
    assert leader_cols == {c.name for c in LeaderLease.__table__.columns}


def test_job_foreign_key_to_tenants(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    fks = insp.get_foreign_keys("jobs")
    assert any(
        fk["referred_table"] == "tenants" and fk["constrained_columns"] == ["tenant_id"]
        for fk in fks
    )


def test_job_lease_foreign_key_to_jobs(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    fks = insp.get_foreign_keys("job_leases")
    assert any(
        fk["referred_table"] == "jobs" and fk["constrained_columns"] == ["job_id"] for fk in fks
    )


def test_due_job_indexes_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    index_names = {ix["name"] for ix in insp.get_indexes("jobs")}
    assert "ix_jobs_due" in index_names
    assert "ix_jobs_tenant_status" in index_names
    assert "ix_jobs_role_status_run_after" in index_names


def test_lease_indexes_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    job_lease_ix = {ix["name"] for ix in insp.get_indexes("job_leases")}
    leader_ix = {ix["name"] for ix in insp.get_indexes("leader_leases")}
    assert "ix_job_leases_leased_until" in job_lease_ix
    assert "ix_job_leases_lease_owner" in job_lease_ix
    assert "ix_leader_leases_leased_until" in leader_ix


def test_model_check_constraints_include_json_valid() -> None:
    """ORM declares json_valid / status / role checks for the jobs table."""
    job_table = Job.__table__
    assert isinstance(job_table, Table)
    check_constraints = [ck for ck in job_table.constraints if isinstance(ck, CheckConstraint)]
    sql_texts = [str(ck.sqltext) for ck in check_constraints]
    assert any("json_valid" in sql for sql in sql_texts)
    assert any("role IN" in sql for sql in sql_texts)
    assert any("status IN" in sql for sql in sql_texts)


def test_leader_lease_model_check_constraints() -> None:
    """ORM declares nonempty name and owner/expiry pair for leader_leases."""
    leader_table = LeaderLease.__table__
    assert isinstance(leader_table, Table)
    check_constraints = [ck for ck in leader_table.constraints if isinstance(ck, CheckConstraint)]
    sql_texts = [str(ck.sqltext) for ck in check_constraints]
    assert any("length(lease_name)" in sql for sql in sql_texts)
    assert any("lease_owner IS NULL AND leased_until IS NULL" in sql for sql in sql_texts)
    assert any("lease_owner IS NOT NULL AND leased_until IS NOT NULL" in sql for sql in sql_texts)
