"""Platform foundation ORM models (tenants, jobs, leases, attempts, dead letters).

Job concurrency protocol is implemented by JobRepository against these tables.
IDs are caller-supplied TEXT values (no application UUIDv7 generator yet).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Float,
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
    oauth_states: Mapped[list[OAuthState]] = relationship(
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


class OAuthState(Base):
    """Short-lived OAuth CSRF/PKCE state for one authorize attempt.

    Stores a **hash** of the OAuth ``state`` (never the raw value) and the
    **envelope-encrypted** PKCE ``code_verifier``. Single-use via
    ``consumed_at``; callers must also enforce ``expires_at``.
    """

    __tablename__ = "oauth_states"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    state_hash: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier_ciphertext: Mapped[bytes] = mapped_column(Blob, nullable=False)
    code_verifier_key_version: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    consumed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="oauth_states")

    __table_args__ = (
        CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="oauth_state_provider",
        ),
        CheckConstraint("length(state_hash) > 0", name="oauth_state_hash_nonempty"),
        CheckConstraint(
            "length(code_verifier_ciphertext) > 0",
            name="oauth_state_verifier_nonempty",
        ),
        CheckConstraint(
            "length(code_verifier_key_version) > 0",
            name="oauth_state_key_version_nonempty",
        ),
        CheckConstraint("length(redirect_uri) > 0", name="oauth_state_redirect_uri_nonempty"),
        CheckConstraint("expires_at > created_at", name="oauth_state_expiry_after_creation"),
        UniqueConstraint("state_hash", name="uq_oauth_states_state_hash"),
        Index("ix_oauth_states_expires_at", "expires_at"),
    )


class SyncRun(Base):
    """One fetch execution against a connection."""

    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    stream: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "trigger IN ('schedule', 'webhook', 'manual', 'reconcile', 'initial')",
            name="sync_run_trigger",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'partial')",
            name="sync_run_status",
        ),
        CheckConstraint(
            "stats_json IS NULL OR json_valid(stats_json)",
            name="sync_run_stats_json_valid",
        ),
        Index("ix_sync_runs_connection_started_at", "connection_id", "started_at"),
    )


class RawPayload(Base):
    """Exact vendor transport page. Every response body is retained."""

    __tablename__ = "raw_payload"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    sync_run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    transport_kind: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    stream: Mapped[str] = mapped_column(Text, nullable=False)
    page_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_blob: Mapped[bytes | None] = mapped_column(Blob, nullable=True)
    request_meta_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "transport_kind IN ('sync_fetch', 'webhook_capture')",
            name="raw_payload_transport_kind",
        ),
        CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="raw_payload_provider",
        ),
        CheckConstraint(
            "NOT (payload_json IS NOT NULL AND payload_blob IS NOT NULL)",
            name="raw_payload_body_exclusive",
        ),
        CheckConstraint(
            "payload_json IS NULL OR json_valid(payload_json)",
            name="raw_payload_json_valid",
        ),
        CheckConstraint(
            "json_valid(request_meta_json)",
            name="raw_payload_request_meta_json_valid",
        ),
        CheckConstraint("length(content_hash) > 0", name="raw_payload_content_hash_nonempty"),
        Index("ix_raw_payload_connection_content_hash", "connection_id", "content_hash"),
        Index("ix_raw_payload_tenant_received_at", "tenant_id", "received_at"),
    )


class SyncCursor(Base):
    """Per-stream ingestion progress for one connection."""

    __tablename__ = "sync_cursors"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    stream: Mapped[str] = mapped_column(Text, nullable=False)
    cursor_type: Mapped[str] = mapped_column(Text, nullable=False)
    cursor_value: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_end: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "cursor_type IN ('timestamp', 'page_token', 'resource_id')",
            name="sync_cursor_type",
        ),
        UniqueConstraint("connection_id", "stream", name="uq_sync_cursors_connection_stream"),
    )


class RawObject(Base):
    """Logical identity of a vendor record."""

    __tablename__ = "raw_objects"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    stream: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    # No FK: raw_revisions references raw_objects, so the reverse FK would be
    # a cycle SQLite cannot satisfy on insert.
    current_revision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar')",
            name="raw_object_provider",
        ),
        UniqueConstraint(
            "tenant_id", "provider", "stream", "vendor_record_id", name="uq_raw_objects_identity"
        ),
    )


class RawRevision(Base):
    """Immutable logical version of a vendor record.

    Append-only: never updated in place. No ``normalizer_version`` here by
    design; that belongs on facts, not on raw snapshots.
    """

    __tablename__ = "raw_revisions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    raw_object_id: Mapped[str] = mapped_column(
        Text, ForeignKey("raw_objects.id", ondelete="CASCADE"), nullable=False
    )
    raw_payload_id: Mapped[str] = mapped_column(
        Text, ForeignKey("raw_payload.id", ondelete="RESTRICT"), nullable=False
    )
    sync_run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    revision_n: Mapped[int] = mapped_column(Integer, nullable=False)
    vendor_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    deletion_state: Mapped[str] = mapped_column(Text, nullable=False)
    is_tombstone: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tombstone_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Exact sub-body for this record. Nullable: revisions written before the
    # per-record split fall back to the full transport body.
    slice_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("revision_n >= 1", name="raw_revision_n_pos"),
        CheckConstraint(
            "deletion_state IN ('active', 'vendor_deleted', 'privacy_scrubbed')",
            name="raw_revision_deletion_state",
        ),
        CheckConstraint("is_tombstone IN (0, 1)", name="raw_revision_is_tombstone_bool"),
        CheckConstraint(
            "tombstone_reason IS NULL OR tombstone_reason IN ('vendor_deleted', 'privacy_delete')",
            name="raw_revision_tombstone_reason",
        ),
        CheckConstraint(
            "(is_tombstone = 0 AND tombstone_reason IS NULL) OR "
            "(is_tombstone = 1 AND tombstone_reason IS NOT NULL)",
            name="raw_revision_tombstone_pair",
        ),
        CheckConstraint("length(content_hash) > 0", name="raw_revision_content_hash_nonempty"),
        UniqueConstraint("raw_object_id", "revision_n", name="uq_raw_revisions_object_n"),
        Index("ix_raw_revisions_object_content_hash", "raw_object_id", "content_hash"),
    )


class FactRecord(Base):
    """Header row for one normalized measurement version.

    Facts are versioned, never updated in place: a changed value or lineage
    writes a new ``version_n`` and supersedes the prior row.
    """

    __tablename__ = "fact_records"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("connections.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    utc_instant: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_utc: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_utc: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_offset_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    iana_timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_health_day: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    freshness_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_revision_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("raw_revisions.id", ondelete="SET NULL"), nullable=True
    )
    raw_payload_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("raw_payload.id", ondelete="SET NULL"), nullable=True
    )
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    normalizer_version: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    version_n: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    deletion_state: Mapped[str] = mapped_column(Text, nullable=False)
    exclude_from_load: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "provider IN ('oura', 'google_health', 'polar', 'manual', 'derived')",
            name="fact_provider",
        ),
        CheckConstraint(
            "method IN ('wearable', 'user_entered', 'lab', 'derived')",
            name="fact_method",
        ),
        CheckConstraint(
            "quality IN ('high', 'medium', 'low', 'unknown')",
            name="fact_quality",
        ),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="fact_confidence_range"),
        CheckConstraint("version_n >= 1", name="fact_version_n_pos"),
        CheckConstraint("is_current IN (0, 1)", name="fact_is_current_bool"),
        CheckConstraint("exclude_from_load IN (0, 1)", name="fact_exclude_from_load_bool"),
        CheckConstraint(
            "deletion_state IN ('active', 'vendor_deleted', 'privacy_scrubbed')",
            name="fact_deletion_state",
        ),
        CheckConstraint(
            "(superseded_by IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by IS NOT NULL AND superseded_at IS NOT NULL AND is_current = 0)",
            name="fact_supersede_pair",
        ),
        CheckConstraint(
            "local_health_day IS NULL OR length(local_health_day) = 10",
            name="fact_local_day_format",
        ),
        # Tenant-scoped: a vendor record id is only unique within a tenant.
        Index(
            "ux_fact_records_current",
            "tenant_id",
            "fact_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
        Index(
            "ux_fact_records_tenant_key_version",
            "tenant_id",
            "fact_key",
            "version_n",
            unique=True,
        ),
        Index(
            "ix_fact_records_day_lookup",
            "tenant_id",
            "entity_type",
            "local_health_day",
            "is_current",
        ),
        Index("ix_fact_records_raw_revision", "tenant_id", "raw_revision_id"),
    )


class SleepSession(Base):
    """Typed sleep detail, one-to-one with its fact header."""

    __tablename__ = "sleep_sessions"

    fact_record_id: Mapped[str] = mapped_column(
        Text, ForeignKey("fact_records.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    is_nap: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duration_min: Mapped[float] = mapped_column(Float, nullable=False)
    time_in_bed_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    efficiency_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    light_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    deep_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    rem_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    awake_min: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint("is_nap IN (0, 1)", name="sleep_session_is_nap_bool"),
        CheckConstraint("duration_min >= 0", name="sleep_session_duration_nonneg"),
        CheckConstraint(
            "efficiency_pct IS NULL OR (efficiency_pct >= 0 AND efficiency_pct <= 100)",
            name="sleep_session_efficiency_range",
        ),
    )


class WorkoutSession(Base):
    """Typed workout detail with canonical zone-load, one-to-one with a header.

    ``session_load`` is the **canonical** load computed internally from the HR-
    zone minutes (never a vendor-provided load). Zone minutes are retained so
    the load can be recomputed under a new zone-weight/formula version. The
    daily strain-load is the sum of a day's included sessions (``exclude_from_
    load = 0`` on the fact header), which feeds the prior-load / ACWR path.
    """

    __tablename__ = "workout_sessions"

    fact_record_id: Mapped[str] = mapped_column(
        Text, ForeignKey("fact_records.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    session_load: Mapped[float] = mapped_column(Float, nullable=False)
    zone1_min: Mapped[float] = mapped_column(Float, nullable=False)
    zone2_min: Mapped[float] = mapped_column(Float, nullable=False)
    zone3_min: Mapped[float] = mapped_column(Float, nullable=False)
    zone4_min: Mapped[float] = mapped_column(Float, nullable=False)
    zone5_min: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        CheckConstraint("session_load >= 0", name="workout_load_nonneg"),
        CheckConstraint(
            "zone1_min >= 0 AND zone2_min >= 0 AND zone3_min >= 0 "
            "AND zone4_min >= 0 AND zone5_min >= 0",
            name="workout_zone_minutes_nonneg",
        ),
    )


class OvernightVitals(Base):
    """Typed overnight-vitals detail, one-to-one with its fact header.

    Overnight HRV (RMSSD ms) and resting heart rate (bpm) are scalar metrics
    measured across the principal sleep and keyed to the wake-date. They feed
    the two highest-weight recovery components. At least one of the two is
    present on any row (a row with neither carries no signal).
    """

    __tablename__ = "overnight_vitals"

    fact_record_id: Mapped[str] = mapped_column(
        Text, ForeignKey("fact_records.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    hrv_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    resting_hr_bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature_deviation_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    respiratory_rate_bpm: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint("hrv_ms IS NULL OR hrv_ms >= 0", name="overnight_vitals_hrv_nonneg"),
        CheckConstraint(
            "resting_hr_bpm IS NULL OR resting_hr_bpm >= 0",
            name="overnight_vitals_rhr_nonneg",
        ),
        CheckConstraint(
            "respiratory_rate_bpm IS NULL OR respiratory_rate_bpm >= 0",
            name="overnight_vitals_resp_nonneg",
        ),
        # A row must carry at least one signal; a row with none holds nothing.
        CheckConstraint(
            "hrv_ms IS NOT NULL OR resting_hr_bpm IS NOT NULL "
            "OR temperature_deviation_c IS NOT NULL OR respiratory_rate_bpm IS NOT NULL",
            name="overnight_vitals_at_least_one",
        ),
    )


class DerivationRun(Base):
    """One reproducible derivation of an artifact (a score, anomaly, …).

    Records the exact inputs and versions a derived value was produced from, so
    a served value can be traced without exposing table or raw ids. The opaque
    ``provenance_token_hash`` is what a day-response URL references; the raw
    token is returned once at creation and never stored, so a database dump
    yields no usable token and lookup is an index probe on the hash.
    """

    __tablename__ = "derivation_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)
    local_health_day: Mapped[str | None] = mapped_column(Text, nullable=True)
    formula_version: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_hash: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    freshness_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    as_of_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_token: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('feature', 'baseline', 'score', 'factor', "
            "'anomaly', 'recommendation')",
            name="derivation_artifact_kind",
        ),
        CheckConstraint(
            "status IN ('ok', 'partial', 'insufficient')",
            name="derivation_status",
        ),
        # The opaque token is the public handle in a day response — unguessable
        # so it cannot be enumerated, but a stable reference, so it must be
        # unique. It is not a secret credential; it is stored in the clear.
        UniqueConstraint("provenance_token", name="uq_derivation_token"),
    )


class DerivationInput(Base):
    """One typed input to a derivation run.

    **No polymorphic pointer.** Each input names its source via a nullable typed
    FK plus a ``role``; exactly one typed FK is non-null (SQL CHECK). Only the
    fact FK is live today (features/baselines/selections are not persisted yet),
    so the CHECK covers the columns that exist.
    """

    __tablename__ = "derivation_inputs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    derivation_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("derivation_runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    fact_record_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("fact_records.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # Exactly one typed FK non-null. Only the fact FK exists today, so this
        # reduces to "the fact FK is present"; it widens with new typed columns.
        CheckConstraint("fact_record_id IS NOT NULL", name="derivation_input_one_typed_fk"),
    )


class Anomaly(Base):
    """A tracked anomaly interval for one feature.

    An anomaly opens when a detector's condition holds and clears only after the
    clear condition has held for two consecutive local days. ``ended_on`` is
    null while open; ``is_active`` mirrors that (1 while open). A partial unique
    index keeps at most one **active** anomaly per ``(tenant_id, feature_code)``,
    so re-detection continues the open interval rather than duplicating it.

    ``consecutive_clear_days`` is engine bookkeeping for the 2-day clear rule.
    """

    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    feature_code: Mapped[str] = mapped_column(Text, nullable=False)
    started_on: Mapped[str] = mapped_column(Text, nullable=False)
    ended_on: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    z_like: Mapped[float | None] = mapped_column(Float, nullable=True)
    formula_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    consecutive_clear_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("severity IN ('moderate', 'high')", name="anomaly_severity"),
        CheckConstraint("is_active IN (0, 1)", name="anomaly_is_active_bool"),
        # An active anomaly is open (no end); a closed one has an end date.
        CheckConstraint(
            "(is_active = 1 AND ended_on IS NULL) OR (is_active = 0 AND ended_on IS NOT NULL)",
            name="anomaly_active_open_pair",
        ),
        CheckConstraint("consecutive_clear_days >= 0", name="anomaly_clear_days_nonneg"),
        CheckConstraint("length(started_on) = 10", name="anomaly_started_format"),
        Index(
            "ux_anomalies_active",
            "tenant_id",
            "feature_code",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )


class SubjectiveCheckIn(Base):
    """A user's completed daily check-in, feeding the subjective component.

    Only a **completed** row (``completed_at`` non-null) is an engine input; an
    absent or incomplete check-in omits the subjective component (never a
    neutral midpoint). Versioned like facts: one current row per tenant/day.

    Values are stored already-normalized to [0, 1]: energy higher is better,
    stress higher is worse, symptom burden higher is worse. A null field is
    unanswered (omit the component); ``symptom_burden_n = 0`` is a real "no
    symptoms" reading.
    """

    __tablename__ = "subjective_check_ins"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    local_health_day: Mapped[str] = mapped_column(Text, nullable=False)
    energy_n: Mapped[float | None] = mapped_column(Float, nullable=True)
    stress_n: Mapped[float | None] = mapped_column(Float, nullable=True)
    symptom_burden_n: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_n: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "energy_n IS NULL OR (energy_n >= 0 AND energy_n <= 1)",
            name="checkin_energy_range",
        ),
        CheckConstraint(
            "stress_n IS NULL OR (stress_n >= 0 AND stress_n <= 1)",
            name="checkin_stress_range",
        ),
        CheckConstraint(
            "symptom_burden_n IS NULL OR (symptom_burden_n >= 0 AND symptom_burden_n <= 1)",
            name="checkin_symptom_range",
        ),
        CheckConstraint("version_n >= 1", name="checkin_version_n_pos"),
        CheckConstraint("is_current IN (0, 1)", name="checkin_is_current_bool"),
        CheckConstraint(
            "(superseded_by IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by IS NOT NULL AND superseded_at IS NOT NULL AND is_current = 0)",
            name="checkin_supersede_pair",
        ),
        CheckConstraint("length(local_health_day) = 10", name="checkin_local_day_format"),
        Index(
            "ux_subjective_check_ins_current",
            "tenant_id",
            "local_health_day",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )


class DailyHealthScore(Base):
    """A computed daily score for one ``score_code``.

    Versioned, never rewritten in place: a changed value supersedes the prior
    row (``is_current`` 0/1 with a partial unique index), so score history stays
    auditable across formula/policy changes. Only score codes with an accepted
    formula may be written; ``recovery`` under ``general_recovery_v0.1.0`` is the
    only one that ships in v0.1.0.
    """

    __tablename__ = "daily_health_scores"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    local_health_day: Mapped[str] = mapped_column(Text, nullable=False)
    score_code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    formula_version: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_hash: Mapped[str] = mapped_column(Text, nullable=False)
    freshness_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    as_of_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    derivation_run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("derivation_runs.id", ondelete="SET NULL"), nullable=True
    )
    version_n: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "score_code IN ('recovery', 'sleep', 'strain', 'activity', 'readiness')",
            name="score_code_registry",
        ),
        CheckConstraint(
            "status IN ('ok', 'partial', 'insufficient')",
            name="score_status",
        ),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="score_range",
        ),
        # A null score is exactly the insufficient case, and vice versa.
        CheckConstraint(
            "(status = 'insufficient') = (score IS NULL)",
            name="score_null_iff_insufficient",
        ),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="score_confidence_range"),
        CheckConstraint("version_n >= 1", name="score_version_n_pos"),
        CheckConstraint("is_current IN (0, 1)", name="score_is_current_bool"),
        CheckConstraint(
            "(superseded_by IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by IS NOT NULL AND superseded_at IS NOT NULL AND is_current = 0)",
            name="score_supersede_pair",
        ),
        CheckConstraint("length(local_health_day) = 10", name="score_local_day_format"),
        Index(
            "ux_daily_health_scores_current",
            "tenant_id",
            "local_health_day",
            "score_code",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )


class ScoreFactor(Base):
    """A signed contributor to a score's derivation, for disclosure."""

    __tablename__ = "score_factors"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    daily_health_score_id: Mapped[str] = mapped_column(
        Text, ForeignKey("daily_health_scores.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    factor_code: Mapped[str] = mapped_column(Text, nullable=False)
    sign: Mapped[int] = mapped_column(Integer, nullable=False)
    magnitude: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    present: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("sign IN (-1, 0, 1)", name="score_factor_sign"),
        CheckConstraint("present IN (0, 1)", name="score_factor_present_bool"),
    )


class DeletionRequest(Base):
    """Privacy deletion pipeline state for one tenant.

    Deliberately has **no** FK to ``tenants``: the request must outlive the
    tenant row it scrubs, or completing a deletion would erase its own audit
    trail.
    """

    __tablename__ = "deletion_requests"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[str] = mapped_column(Text, nullable=False)
    jobs_cancelled_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_scrubbed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    backups_scheduled_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'jobs_cancelled', 'rows_scrubbed', "
            "'backups_scheduled', 'completed', 'failed')",
            name="deletion_request_status",
        ),
        UniqueConstraint("tenant_id", "requested_at", name="uq_deletion_requests_tenant_time"),
        Index("ix_deletion_requests_status", "status", "requested_at"),
    )


class DeletionCompletionProof(Base):
    """Minimal, non-identifying proof that a deletion completed.

    Counts only: no tenant id, no identity, no health values.
    """

    __tablename__ = "deletion_completion_proofs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    deletion_request_id: Mapped[str] = mapped_column(
        Text, ForeignKey("deletion_requests.id", ondelete="CASCADE"), nullable=False
    )
    completed_at: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scrub_counts_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('completed', 'partial')", name="deletion_proof_status"),
        CheckConstraint("json_valid(scrub_counts_json)", name="deletion_proof_counts_json_valid"),
        UniqueConstraint("deletion_request_id", name="uq_deletion_proofs_request"),
    )


class User(Base):
    """A person, identified by an OIDC issuer/subject pair."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    oidc_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    oidc_subject: Mapped[str] = mapped_column(Text, nullable=False)
    # Sensitive PII: never free log material.
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("length(oidc_issuer) > 0", name="user_issuer_nonempty"),
        CheckConstraint("length(oidc_subject) > 0", name="user_subject_nonempty"),
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_issuer_subject"),
        Index("ix_users_tenant", "tenant_id"),
    )


class SessionRow(Base):
    """A backend-issued opaque session.

    Named ``SessionRow`` to avoid colliding with SQLAlchemy's ``Session``.
    Stores hashes only: the raw cookie token and CSRF secret are returned to
    the caller once at issue time and never persisted.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("length(token_hash) > 0", name="session_token_hash_nonempty"),
        CheckConstraint("length(csrf_secret_hash) > 0", name="session_csrf_hash_nonempty"),
        CheckConstraint("expires_at > created_at", name="session_expiry_after_creation"),
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        Index("ix_sessions_user", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )


class LoginState(Base):
    """One in-flight OIDC login attempt.

    Separate from ``OAuthState``: at login time no tenant exists yet, and OIDC
    needs a ``nonce`` that the connector flow has no use for. ``state`` and
    ``nonce`` are stored hashed; the PKCE verifier is envelope-encrypted.
    """

    __tablename__ = "login_states"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    state_hash: Mapped[str] = mapped_column(Text, nullable=False)
    nonce_hash: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier_ciphertext: Mapped[bytes] = mapped_column(Blob, nullable=False)
    code_verifier_key_version: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    consumed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("length(state_hash) > 0", name="login_state_hash_nonempty"),
        CheckConstraint("length(nonce_hash) > 0", name="login_state_nonce_hash_nonempty"),
        CheckConstraint(
            "length(code_verifier_ciphertext) > 0", name="login_state_verifier_nonempty"
        ),
        CheckConstraint(
            "length(code_verifier_key_version) > 0", name="login_state_key_version_nonempty"
        ),
        CheckConstraint("length(redirect_uri) > 0", name="login_state_redirect_nonempty"),
        CheckConstraint("expires_at > created_at", name="login_state_expiry_after_creation"),
        UniqueConstraint("state_hash", name="uq_login_states_state_hash"),
        Index("ix_login_states_expires_at", "expires_at"),
    )


class WebhookInbox(Base):
    """One durable, deduplicated verified webhook delivery."""

    __tablename__ = "webhook_inbox"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[str] = mapped_column(Text, nullable=False)
    headers_meta_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Sole FK between inbox and payload; set after the payload is captured.
    body_payload_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("raw_payload.id", ondelete="SET NULL"), nullable=True
    )
    processing_status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "processing_status IN ('accepted', 'enqueued', 'processed', 'ignored_dup')",
            name="webhook_inbox_status",
        ),
        CheckConstraint("json_valid(headers_meta_json)", name="webhook_inbox_headers_json"),
        UniqueConstraint("connection_id", "dedupe_key", name="uq_webhook_inbox_dedupe"),
    )
