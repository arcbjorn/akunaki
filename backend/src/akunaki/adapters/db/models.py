"""Platform foundation ORM models (tenants, jobs, leases, attempts, dead letters).

Job concurrency protocol is implemented by JobRepository against these tables.
IDs are caller-supplied TEXT values (no application UUIDv7 generator yet).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from akunaki.adapters.db.base import Base
from akunaki.adapters.db.types import Blob


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
    dead_letters_rel: Mapped[list[JobDeadLetter]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    connections: Mapped[list[Connection]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )

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
    job_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'system.noop'"),
    )
    last_error_class: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="jobs")
    lease: Mapped[JobLease | None] = relationship(
        back_populates="job",
        uselist=False,
        cascade="all, delete-orphan",
    )
    attempts_rel: Mapped[list[JobAttempt]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    dead_letter: Mapped[JobDeadLetter | None] = relationship(
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
        Index("ix_jobs_role_job_type_status_run_after", "role", "job_type", "status", "run_after"),
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


class JobAttempt(Base):
    """Per-attempt tracking row for durable job execution."""

    __tablename__ = "job_attempts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    redacted_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[Job] = relationship(back_populates="attempts_rel")

    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="job_attempt_number_pos"),
        CheckConstraint("fence_token >= 0", name="job_attempt_fence_token_nonneg"),
        CheckConstraint("length(lease_owner) > 0", name="job_attempt_lease_owner_nonempty"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'retry_scheduled', 'dead_letter', 'lease_expired')",
            name="job_attempt_status",
        ),
        UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_job_attempts_job_id_attempt_number",
        ),
        Index("ix_job_attempts_job_id", "job_id"),
        Index("ix_job_attempts_status", "status"),
    )


class JobDeadLetter(Base):
    """Permanent failure record for a dead-lettered job."""

    __tablename__ = "job_dead_letters"

    job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False)
    error_class: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_lettered_at: Mapped[str] = mapped_column(Text, nullable=False)

    job: Mapped[Job] = relationship(back_populates="dead_letter")
    tenant: Mapped[Tenant] = relationship(back_populates="dead_letters_rel")

    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="job_dl_attempt_number_pos"),
        CheckConstraint("fence_token >= 0", name="job_dl_fence_token_nonneg"),
        Index("ix_job_dead_letters_tenant_dead_lettered_at", "tenant_id", "dead_lettered_at"),
    )


class Connection(Base):
    """Per-tenant provider connection.

    Tokens are never stored here: secret material lives in
    :class:`ConnectionSecret` as envelope-encrypted ciphertext.
    """

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scopes_granted_json: Mapped[str] = mapped_column(Text, nullable=False)
    external_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="connections")
    secret: Mapped[ConnectionSecret | None] = relationship(
        back_populates="connection",
        uselist=False,
        cascade="all, delete-orphan",
    )
    health: Mapped[ConnectionHealth | None] = relationship(
        back_populates="connection",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="connection_provider",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'needs_reauth', 'revoked', 'error')",
            name="connection_status",
        ),
        CheckConstraint(
            "json_valid(scopes_granted_json)",
            name="connection_scopes_json_valid",
        ),
        UniqueConstraint("tenant_id", "provider", name="uq_connections_tenant_provider"),
        Index("ix_connections_tenant_status", "tenant_id", "status"),
    )


class ConnectionSecret(Base):
    """Envelope-encrypted token material for one connection (PK = connection_id)."""

    __tablename__ = "connection_secrets"

    connection_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    ciphertext: Mapped[bytes] = mapped_column(Blob, nullable=False)
    key_version: Mapped[str] = mapped_column(Text, nullable=False)
    rotated_at: Mapped[str] = mapped_column(Text, nullable=False)

    connection: Mapped[Connection] = relationship(back_populates="secret")

    __table_args__ = (
        CheckConstraint("length(ciphertext) > 0", name="connection_secret_ciphertext_nonempty"),
        CheckConstraint("length(key_version) > 0", name="connection_secret_key_version_nonempty"),
    )


class ConnectionHealth(Base):
    """Operational health counters for one connection (PK = connection_id).

    Error classes only; never payload bodies or measurement values.
    """

    __tablename__ = "connection_health"

    connection_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    last_success_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    rate_limit_reset_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_last_verified_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    connection: Mapped[Connection] = relationship(back_populates="health")

    __table_args__ = (
        CheckConstraint("consecutive_failures >= 0", name="connection_health_failures_nonneg"),
    )
