"""Backend-issued sessions: hashed storage, expiry, revocation, rotation.

The central rule is that a database dump must yield no usable session: only
hashes are stored, and lookup is by hash rather than by comparing secrets.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.sessions import (
    TOKEN_PREFIX,
    generate_csrf_secret,
    generate_session_token,
    hash_token,
    token_matches,
)
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import SessionRow, Tenant, User
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.sessions import IssuedSession, SessionRejection

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
ConstraintError = (IntegrityError, ValueError)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def session_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "sessions.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    command.upgrade(cfg, "head")
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(session_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=session_db))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
        session.add(
            User(
                id="user-1",
                tenant_id="tenant-1",
                oidc_issuer="https://idp.example.com",
                oidc_subject="subject-1",
                email="person@example.com",
                created_at=NOW_S,
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def repository(factory: sessionmaker[Session]) -> SessionRepository:
    return SessionRepository(factory)


def _issue(
    repository: SessionRepository,
    *,
    session_id: str = "sess-1",
    now: datetime = T0,
    ttl: timedelta = timedelta(hours=12),
) -> IssuedSession:
    return repository.issue(session_id=session_id, user_id="user-1", now=now, ttl=ttl)


# ---------------------------------------------------------------------------
# Token generation (pure)
# ---------------------------------------------------------------------------


def test_tokens_are_unique_and_prefixed() -> None:
    tokens = {generate_session_token() for _ in range(256)}
    assert len(tokens) == 256
    assert all(t.startswith(TOKEN_PREFIX) for t in tokens)


def test_csrf_secrets_are_unique() -> None:
    assert len({generate_csrf_secret() for _ in range(256)}) == 256


def test_hash_hides_the_token() -> None:
    token = generate_session_token()
    hashed = hash_token(token)

    assert token not in hashed
    assert len(hashed) == 64  # sha256 hex


def test_token_matching_is_exact() -> None:
    token = generate_session_token()
    assert token_matches(token, hash_token(token)) is True
    assert token_matches(generate_session_token(), hash_token(token)) is False
    assert token_matches("", hash_token(token)) is False
    assert token_matches(token, "") is False


def test_hash_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        hash_token("")


# ---------------------------------------------------------------------------
# Storage secrecy
# ---------------------------------------------------------------------------


def test_raw_token_is_never_stored(repository: SessionRepository, session_db: str) -> None:
    """A database dump must yield no usable session."""
    issued = _issue(repository)

    engine = create_db_engine(Settings(database_url=session_db))
    try:
        with engine.connect() as conn:
            stored_hash, stored_csrf = conn.execute(
                text("SELECT token_hash, csrf_secret_hash FROM sessions")
            ).one()
    finally:
        engine.dispose()

    assert stored_hash == hash_token(issued.token)
    assert issued.token not in stored_hash
    assert issued.csrf_secret not in stored_csrf


def test_issued_session_repr_redacts_secrets() -> None:
    issued = IssuedSession(
        session_id="s",
        user_id="u",
        tenant_id="t",
        token="aks_SECRET",
        csrf_secret="CSRF_SECRET",
        expires_at=NOW_S,
    )
    rendered = repr(issued)

    assert "aks_SECRET" not in rendered
    assert "CSRF_SECRET" not in rendered
    assert "<redacted>" in rendered


def test_duplicate_token_hash_is_rejected(
    repository: SessionRepository, factory: sessionmaker[Session]
) -> None:
    issued = _issue(repository)
    with pytest.raises(ConstraintError), factory() as session, session.begin():
        session.add(
            SessionRow(
                id="sess-2",
                user_id="user-1",
                tenant_id="tenant-1",
                token_hash=hash_token(issued.token),
                csrf_secret_hash="x",
                created_at=NOW_S,
                expires_at=to_utc_rfc3339(T0 + timedelta(hours=1)),
            )
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_token_authenticates(repository: SessionRepository) -> None:
    issued = _issue(repository)
    result = repository.validate(token=issued.token, now=T0 + timedelta(minutes=1))

    assert result.ok
    assert result.session is not None
    assert result.session.user_id == "user-1"
    assert result.session.tenant_id == "tenant-1"


def test_unknown_token_is_rejected(repository: SessionRepository) -> None:
    _issue(repository)
    result = repository.validate(token=generate_session_token(), now=T0)

    assert result.rejection is SessionRejection.NOT_FOUND
    assert result.session is None


def test_empty_token_is_rejected(repository: SessionRepository) -> None:
    assert repository.validate(token="", now=T0).rejection is SessionRejection.NOT_FOUND


def test_expired_session_is_rejected(repository: SessionRepository) -> None:
    issued = _issue(repository, ttl=timedelta(minutes=30))
    result = repository.validate(token=issued.token, now=T0 + timedelta(hours=1))

    assert result.rejection is SessionRejection.EXPIRED


def test_expiry_boundary_is_exclusive(repository: SessionRepository) -> None:
    issued = _issue(repository, ttl=timedelta(minutes=30))
    at_expiry = repository.validate(token=issued.token, now=T0 + timedelta(minutes=30))

    assert at_expiry.rejection is SessionRejection.EXPIRED


def test_revoked_session_is_rejected(repository: SessionRepository) -> None:
    issued = _issue(repository)
    assert repository.revoke(session_id=issued.session_id, now=T0) is True

    result = repository.validate(token=issued.token, now=T0)
    assert result.rejection is SessionRejection.REVOKED


def test_revocation_is_idempotent(repository: SessionRepository) -> None:
    issued = _issue(repository)
    assert repository.revoke(session_id=issued.session_id, now=T0) is True
    # Already revoked: no second effect.
    assert repository.revoke(session_id=issued.session_id, now=T0) is False


def test_revoking_unknown_session_returns_false(repository: SessionRepository) -> None:
    assert repository.revoke(session_id="nope", now=T0) is False


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_csrf_secret_verifies(repository: SessionRepository) -> None:
    issued = _issue(repository)
    assert (
        repository.verify_csrf(session_id=issued.session_id, csrf_secret=issued.csrf_secret) is True
    )


def test_wrong_csrf_secret_is_rejected(repository: SessionRepository) -> None:
    issued = _issue(repository)
    assert (
        repository.verify_csrf(session_id=issued.session_id, csrf_secret=generate_csrf_secret())
        is False
    )
    assert repository.verify_csrf(session_id=issued.session_id, csrf_secret="") is False


def test_csrf_secret_of_another_session_is_rejected(
    repository: SessionRepository,
) -> None:
    """A CSRF secret must be bound to its own session."""
    first = _issue(repository, session_id="sess-1")
    second = _issue(repository, session_id="sess-2")

    assert (
        repository.verify_csrf(session_id=first.session_id, csrf_secret=second.csrf_secret) is False
    )


def test_csrf_for_unknown_session_is_rejected(repository: SessionRepository) -> None:
    assert repository.verify_csrf(session_id="nope", csrf_secret="x") is False


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotation_issues_a_new_token_and_revokes_the_old(
    repository: SessionRepository,
) -> None:
    """A session identifier must not survive a privilege change."""
    original = _issue(repository)
    rotated = repository.rotate(old_token=original.token, new_session_id="sess-2", now=T0)

    assert rotated is not None
    assert rotated.token != original.token
    assert rotated.csrf_secret != original.csrf_secret

    # The old cookie stops working; the new one works.
    assert repository.validate(token=original.token, now=T0).rejection is (SessionRejection.REVOKED)
    assert repository.validate(token=rotated.token, now=T0).ok


def test_rotation_preserves_identity(repository: SessionRepository) -> None:
    original = _issue(repository)
    rotated = repository.rotate(old_token=original.token, new_session_id="sess-2", now=T0)

    assert rotated is not None
    assert rotated.user_id == original.user_id
    assert rotated.tenant_id == original.tenant_id


def test_rotating_an_invalid_session_yields_nothing(
    repository: SessionRepository,
) -> None:
    assert (
        repository.rotate(old_token=generate_session_token(), new_session_id="sess-2", now=T0)
        is None
    )


def test_rotating_a_revoked_session_yields_nothing(
    repository: SessionRepository,
) -> None:
    issued = _issue(repository)
    repository.revoke(session_id=issued.session_id, now=T0)

    assert repository.rotate(old_token=issued.token, new_session_id="sess-2", now=T0) is None


# ---------------------------------------------------------------------------
# Bulk revocation and purge
# ---------------------------------------------------------------------------


def test_logout_everywhere_revokes_all_live_sessions(
    repository: SessionRepository,
) -> None:
    first = _issue(repository, session_id="sess-1")
    second = _issue(repository, session_id="sess-2")

    assert repository.revoke_all_for_user(user_id="user-1", now=T0) == 2
    assert repository.validate(token=first.token, now=T0).rejection is (SessionRejection.REVOKED)
    assert repository.validate(token=second.token, now=T0).rejection is (SessionRejection.REVOKED)


def test_purge_removes_only_expired_sessions(
    repository: SessionRepository, factory: sessionmaker[Session]
) -> None:
    _issue(repository, session_id="old", ttl=timedelta(minutes=5))
    _issue(repository, session_id="live", ttl=timedelta(hours=6))

    removed = repository.purge_expired(now=T0 + timedelta(hours=1))

    assert removed == 1
    with factory() as session:
        remaining = session.scalars(select(SessionRow.id)).all()
    assert list(remaining) == ["live"]


# ---------------------------------------------------------------------------
# Validation of inputs and schema invariants
# ---------------------------------------------------------------------------


def test_issue_requires_a_known_user(repository: SessionRepository) -> None:
    with pytest.raises(ValueError, match="not found"):
        repository.issue(session_id="s", user_id="ghost", now=T0)


def test_issue_validates_arguments(repository: SessionRepository) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        repository.issue(session_id="", user_id="user-1", now=T0)
    with pytest.raises(ValueError, match="ttl must be at least one second"):
        repository.issue(session_id="s", user_id="user-1", now=T0, ttl=timedelta(0))
    with pytest.raises(ValueError, match="must be timezone-aware"):
        repository.issue(
            session_id="s",
            user_id="user-1",
            now=datetime(2026, 7, 19, 12, 0, 0),
        )


def test_user_identity_is_unique_per_issuer_subject(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(ConstraintError), factory() as session, session.begin():
        session.add(
            User(
                id="user-2",
                tenant_id="tenant-1",
                oidc_issuer="https://idp.example.com",
                oidc_subject="subject-1",
                email=None,
                created_at=NOW_S,
            )
        )


def test_sessions_cascade_with_the_user(
    repository: SessionRepository, factory: sessionmaker[Session]
) -> None:
    _issue(repository)

    with factory() as session, session.begin():
        user = session.get(User, "user-1")
        assert user is not None
        session.delete(user)

    with factory() as session:
        assert session.scalars(select(SessionRow)).all() == []
