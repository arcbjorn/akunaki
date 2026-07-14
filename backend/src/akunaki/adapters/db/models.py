"""Platform foundation ORM models (tenants, jobs, leases).

Job concurrency protocol is implemented by JobRepository against these tables.
IDs are caller-supplied TEXT values (no application UUIDv7 generator yet).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from akunaki.adapters.db.base import Base


class Tenant(Base):
    """Tenant row (platform foundation only)."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    primary_timezone: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'UTC'"),
    )
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    jobs: Mapped[list[Job]] = relationship(back_populates="tenant")

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'pending_delete')",
            name="tenant_status",
        ),
    )


class Job(Base):
    """Durable work unit row.

    Claim/lease operations live in JobRepository (CAS; no FOR UPDATE).
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    run_after: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="jobs")
    lease: Mapped[JobLease | None] = relationship(
        back_populates="job",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('core', 'agent')",
            name="job_role",
        ),
        CheckConstraint(
            "status IN ('ready', 'leased', 'succeeded', 'failed', 'cancelled', 'dead_letter')",
            name="job_status",
        ),
        CheckConstraint(
            "json_valid(payload_json)",
            name="job_payload_json_valid",
        ),
        CheckConstraint("attempts >= 0", name="job_attempts_nonneg"),
        CheckConstraint("max_attempts >= 1", name="job_max_attempts_pos"),
        CheckConstraint("fence_token >= 0", name="job_fence_token_nonneg"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_jobs_tenant_idempotency_key",
        ),
        Index(
            "ix_jobs_due",
            "status",
            "run_after",
            "priority",
            "created_at",
        ),
        Index("ix_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_jobs_role_status_run_after", "role", "status", "run_after"),
    )


class JobLease(Base):
    """One active lease row per job (PK = job_id)."""

    __tablename__ = "job_leases"

    job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    lease_owner: Mapped[str] = mapped_column(Text, nullable=False)
    leased_until: Mapped[str] = mapped_column(Text, nullable=False)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    job: Mapped[Job] = relationship(back_populates="lease")

    __table_args__ = (
        CheckConstraint("fence_token >= 0", name="job_lease_fence_token_nonneg"),
        CheckConstraint("length(lease_owner) > 0", name="job_lease_owner_nonempty"),
        Index("ix_job_leases_leased_until", "leased_until"),
        Index("ix_job_leases_lease_owner", "lease_owner"),
    )


class LeaderLease(Base):
    """Named leader coordination row with optional owner and fencing token.

    ``lease_owner`` and ``leased_until`` are both null (free) or both non-null
    (held). ``lease_name`` must be nonempty.
    """

    __tablename__ = "leader_leases"

    lease_name: Mapped[str] = mapped_column(Text, primary_key=True)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    leased_until: Mapped[str | None] = mapped_column(Text, nullable=True)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("fence_token >= 0", name="leader_lease_fence_token_nonneg"),
        CheckConstraint("length(lease_name) > 0", name="leader_lease_name_nonempty"),
        CheckConstraint(
            "(lease_owner IS NULL AND leased_until IS NULL) OR "
            "(lease_owner IS NOT NULL AND leased_until IS NOT NULL)",
            name="leader_lease_owner_expiry_pair",
        ),
        CheckConstraint(
            "lease_owner IS NULL OR length(lease_owner) > 0",
            name="leader_lease_owner_null_or_nonempty",
        ),
        Index("ix_leader_leases_leased_until", "leased_until"),
    )
