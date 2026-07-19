"""Database adapter: SQLAlchemy engine, sessions, and ORM models."""

from akunaki.adapters.db.base import NAMING_CONVENTION, Base
from akunaki.adapters.db.engine import (
    create_db_engine,
    create_session_factory,
    probe_database_ready,
)
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.models import (
    Connection,
    ConnectionHealth,
    ConnectionSecret,
    Job,
    JobAttempt,
    JobDeadLetter,
    JobLease,
    LeaderLease,
    OAuthState,
    RawObject,
    RawPayload,
    RawRevision,
    SyncCursor,
    SyncRun,
    Tenant,
)
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Connection",
    "ConnectionHealth",
    "ConnectionSecret",
    "Job",
    "JobAttempt",
    "JobDeadLetter",
    "JobLease",
    "JobRepository",
    "LeaderLease",
    "OAuthState",
    "OAuthStateRepository",
    "RawObject",
    "RawPayload",
    "RawRevision",
    "SyncCursor",
    "SyncRun",
    "Tenant",
    "create_db_engine",
    "create_session_factory",
    "probe_database_ready",
]
