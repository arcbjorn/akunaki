"""Pure domain types for durable job leases and leader fencing.

No I/O, no SQLAlchemy, no framework imports. Timestamps that cross this
boundary are timezone-aware datetimes or canonical UTC RFC3339 text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class JobRole(StrEnum):
    """Worker role that may claim a job."""

    CORE = "core"
    AGENT = "agent"


class JobStatus(StrEnum):
    """Durable job lifecycle status."""

    READY = "ready"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


class JobAttemptStatus(StrEnum):
    """Execution outcome recorded for one durable job attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTER = "dead_letter"
    LEASE_EXPIRED = "lease_expired"


class JobFailureDisposition(StrEnum):
    """Repository disposition after recording a job failure."""

    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


def require_aware(dt: datetime, *, field_name: str = "datetime") -> datetime:
    """Reject naive datetimes; return the same instance when timezone-aware."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        msg = f"{field_name} must be timezone-aware; naive datetimes are rejected"
        raise ValueError(msg)
    return dt


def to_utc_rfc3339(dt: datetime) -> str:
    """Serialize a timezone-aware datetime to a single sortable UTC RFC3339 form.

    Always UTC with a ``Z`` suffix and fixed **second** precision so string
    comparison matches chronological order for this representation (and for
    existing foundation rows stored without fractional seconds). Lease TTLs
    must therefore be at least one second; a positive subsecond TTL would
    serialize to immediate expiry.
    """
    aware = require_aware(dt, field_name="dt")
    utc = aware.astimezone(UTC).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_rfc3339(value: str) -> datetime:
    """Parse a UTC RFC3339 string produced by :func:`to_utc_rfc3339` (or compatible)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return require_aware(parsed, field_name="value").astimezone(UTC)


@dataclass(frozen=True, slots=True)
class JobCandidate:
    """Non-locking discovery result for a due ready job.

    ``expected_fence_token`` is the fence observed at discovery time and must
    match on the conditional claim UPDATE.
    """

    job_id: str
    tenant_id: str
    role: JobRole
    expected_fence_token: int
    priority: int
    run_after: str
    attempts: int
    max_attempts: int
    created_at: str


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    """Result of an enqueue request.

    ``created`` is False when an existing job with the same
    ``(tenant_id, idempotency_key)`` was returned instead of inserting a
    duplicate, so callers can distinguish a fresh enqueue from a deduped one.
    """

    job_id: str
    tenant_id: str
    job_type: str
    role: JobRole
    created: bool


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Winning claim after a successful CAS lease acquisition."""

    job_id: str
    tenant_id: str
    role: JobRole
    job_type: str
    owner: str
    fence_token: int
    leased_until: str
    attempts: int
    max_attempts: int
    payload_json: str


@dataclass(frozen=True, slots=True)
class JobFailureResult:
    """Committed result of handling a leased job failure."""

    disposition: JobFailureDisposition
    job_id: str
    attempt_number: int
    fence_token: int
    run_after: str | None = None


@dataclass(frozen=True, slots=True)
class LeaderClaim:
    """Winning claim of a named leader lease."""

    lease_name: str
    owner: str
    fence_token: int
    leased_until: str
