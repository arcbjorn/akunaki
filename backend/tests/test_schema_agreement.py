"""Migration DDL and ORM models agree on the foundation schema."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import Table

from akunaki.adapters.db.models import Job, JobAttempt, JobDeadLetter, JobLease, LeaderLease, Tenant


def test_tables_match_models(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert set(insp.get_table_names()) >= {
        "tenants",
        "jobs",
        "job_leases",
        "leader_leases",
        "job_attempts",
        "job_dead_letters",
        "alembic_version",
    }

    tenant_cols = {c["name"] for c in insp.get_columns("tenants")}
    job_cols = {c["name"] for c in insp.get_columns("jobs")}
    lease_cols = {c["name"] for c in insp.get_columns("job_leases")}
    leader_cols = {c["name"] for c in insp.get_columns("leader_leases")}
    attempt_cols = {c["name"] for c in insp.get_columns("job_attempts")}
    dead_letter_cols = {c["name"] for c in insp.get_columns("job_dead_letters")}

    assert tenant_cols == {c.name for c in Tenant.__table__.columns}
    assert job_cols == {c.name for c in Job.__table__.columns}
    assert lease_cols == {c.name for c in JobLease.__table__.columns}
    assert leader_cols == {c.name for c in LeaderLease.__table__.columns}
    assert attempt_cols == {c.name for c in JobAttempt.__table__.columns}
    assert dead_letter_cols == {c.name for c in JobDeadLetter.__table__.columns}


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
    assert "ix_jobs_role_job_type_status_run_after" in index_names


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


def test_job_attempt_foreign_key_to_jobs(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    fks = insp.get_foreign_keys("job_attempts")
    assert any(
        fk["referred_table"] == "jobs" and fk["constrained_columns"] == ["job_id"] for fk in fks
    )


def test_job_dead_letter_foreign_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    fks = insp.get_foreign_keys("job_dead_letters")
    assert any(
        fk["referred_table"] == "jobs" and fk["constrained_columns"] == ["job_id"] for fk in fks
    )
    assert any(
        fk["referred_table"] == "tenants" and fk["constrained_columns"] == ["tenant_id"]
        for fk in fks
    )


def test_job_attempt_indexes_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    attempt_ix = {ix["name"] for ix in insp.get_indexes("job_attempts")}
    assert "ix_job_attempts_job_id" in attempt_ix
    assert "ix_job_attempts_status" in attempt_ix


def test_job_dead_letter_indexes_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    dl_ix = {ix["name"] for ix in insp.get_indexes("job_dead_letters")}
    assert "ix_job_dead_letters_tenant_dead_lettered_at" in dl_ix


def test_job_attempt_model_check_constraints() -> None:
    """ORM declares attempt_number positive, fence_token nonneg, owner nonempty, status."""
    attempt_table = JobAttempt.__table__
    assert isinstance(attempt_table, Table)
    check_constraints = [ck for ck in attempt_table.constraints if isinstance(ck, CheckConstraint)]
    sql_texts = [str(ck.sqltext) for ck in check_constraints]
    assert any("attempt_number >= 1" in sql for sql in sql_texts)
    assert any("fence_token >= 0" in sql for sql in sql_texts)
    assert any("length(lease_owner)" in sql for sql in sql_texts)
    assert any("status IN" in sql for sql in sql_texts)


def test_job_dead_letter_model_check_constraints() -> None:
    """ORM declares attempt_number positive, fence_token nonneg for dead letters."""
    dl_table = JobDeadLetter.__table__
    assert isinstance(dl_table, Table)
    check_constraints = [ck for ck in dl_table.constraints if isinstance(ck, CheckConstraint)]
    sql_texts = [str(ck.sqltext) for ck in check_constraints]
    assert any("attempt_number >= 1" in sql for sql in sql_texts)
    assert any("fence_token >= 0" in sql for sql in sql_texts)
