"""Session persistence: issue, validate, rotate, revoke.

The raw cookie token is never written. It is generated here, returned once to
the caller, and only its hash is stored — so lookup is an index probe on the
hash and a database dump yields no usable session.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.sessions import (
    generate_csrf_secret,
    generate_session_token,
    hash_token,
    token_matches,
)
from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import SessionRow, User
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.sessions import (
    AuthenticatedSession,
    IssuedSession,
    SessionRejection,
    SessionValidation,
)

DEFAULT_SESSION_TTL = timedelta(hours=12)
MIN_SESSION_TTL = timedelta(seconds=1)


class SessionRepository:
    """Issue and validate backend-issued opaque sessions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def issue(
        self,
        *,
        session_id: str,
        user_id: str,
        now: datetime,
        ttl: timedelta = DEFAULT_SESSION_TTL,
    ) -> IssuedSession:
        """Create a session, returning its secrets **once**."""
        if not session_id or not user_id:
            msg = "session_id and user_id must be non-empty"
            raise ValueError(msg)
        if ttl < MIN_SESSION_TTL:
            msg = "ttl must be at least one second (second-resolution timestamps)"
            raise ValueError(msg)

        now_aware = require_aware(now, field_name="now")
        created_at = to_utc_rfc3339(now_aware)
        expires_at = to_utc_rfc3339(now_aware + ttl)
        token = generate_session_token()
        csrf_secret = generate_csrf_secret()

        with self._session_factory() as session, session.begin():
            tenant_id = session.execute(
                select(User.tenant_id).where(User.id == user_id)
            ).scalar_one_or_none()
            if tenant_id is None:
                msg = f"user {user_id!r} not found"
                raise ValueError(msg)

            session.add(
                SessionRow(
                    id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    token_hash=hash_token(token),
                    csrf_secret_hash=hash_token(csrf_secret),
                    created_at=created_at,
                    expires_at=expires_at,
                    revoked_at=None,
                )
            )

        return IssuedSession(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            token=token,
            csrf_secret=csrf_secret,
            expires_at=expires_at,
        )

    def validate(self, *, token: str, now: datetime) -> SessionValidation:
        """Validate a presented cookie token.

        Returns a typed rejection rather than raising, so callers surface one
        generic ``401`` without revealing which check failed.
        """
        if not token:
            return SessionValidation(rejection=SessionRejection.NOT_FOUND)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session:
            row = session.execute(
                select(
                    SessionRow.id,
                    SessionRow.user_id,
                    SessionRow.tenant_id,
                    SessionRow.expires_at,
                    SessionRow.revoked_at,
                ).where(SessionRow.token_hash == hash_token(token))
            ).one_or_none()
            if row is None:
                return SessionValidation(rejection=SessionRejection.NOT_FOUND)

            session_id, user_id, tenant_id, expires_at, revoked_at = row
            if revoked_at is not None:
                return SessionValidation(rejection=SessionRejection.REVOKED)
            if expires_at <= now_s:
                return SessionValidation(rejection=SessionRejection.EXPIRED)

            return SessionValidation(
                session=AuthenticatedSession(
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    expires_at=expires_at,
                )
            )

    def verify_csrf(self, *, session_id: str, csrf_secret: str) -> bool:
        """Constant-time CSRF check for a cookie-authenticated mutation."""
        if not csrf_secret:
            return False
        with self._session_factory() as session:
            stored = session.execute(
                select(SessionRow.csrf_secret_hash).where(SessionRow.id == session_id)
            ).scalar_one_or_none()
        if stored is None:
            return False
        return token_matches(csrf_secret, stored)

    def rotate(
        self,
        *,
        old_token: str,
        new_session_id: str,
        now: datetime,
        ttl: timedelta = DEFAULT_SESSION_TTL,
    ) -> IssuedSession | None:
        """Issue a successor session and revoke the old one.

        Rotation on privilege change is a design requirement: a session
        identifier must not survive a change in what it authorizes.
        """
        validation = self.validate(token=old_token, now=now)
        if not validation.ok or validation.session is None:
            return None

        issued = self.issue(
            session_id=new_session_id,
            user_id=validation.session.user_id,
            now=now,
            ttl=ttl,
        )
        # Revoke only after the successor exists, so a crash between the two
        # leaves the user logged in rather than stranded.
        self.revoke(session_id=validation.session.session_id, now=now)
        return issued

    def revoke(self, *, session_id: str, now: datetime) -> bool:
        """Revoke one session. False when unknown or already revoked."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id, SessionRow.revoked_at.is_(None))
                .values(revoked_at=now_s)
            )
            return affected_rows(result) == 1

    def revoke_all_for_user(self, *, user_id: str, now: datetime) -> int:
        """Revoke every live session for a user (logout everywhere)."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(SessionRow)
                .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
                .values(revoked_at=now_s)
            )
            return affected_rows(result)

    def purge_expired(self, *, now: datetime) -> int:
        """Delete sessions past their expiry. Returns rows removed."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            result = session.execute(delete(SessionRow).where(SessionRow.expires_at <= now_s))
            return affected_rows(result)
