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
    FactRecord,
    Job,
    JobAttempt,
    JobDeadLetter,
    JobLease,
    LeaderLease,
    LoginState,
    OAuthState,
    RawObject,
    RawPayload,
    RawRevision,
    SessionRow,
    SleepSession,
    SyncCursor,
    SyncRun,
    Tenant,
    User,
)
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Connection",
    "ConnectionHealth",
    "ConnectionSecret",
    "FactRecord",
    "Job",
    "JobAttempt",
    "JobDeadLetter",
    "JobLease",
    "JobRepository",
    "LeaderLease",
    "LoginState",
    "OAuthState",
    "OAuthStateRepository",
    "RawObject",
    "RawPayload",
    "RawRevision",
    "SessionRow",
    "SleepSession",
    "SyncCursor",
    "SyncRun",
    "Tenant",
    "User",
    "create_db_engine",
    "create_session_factory",
    "probe_database_ready",
]
