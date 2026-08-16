"""Service token persistence: issue, validate, revoke.

The raw bearer token is never written. It is generated here, returned once to
the caller, and only its hash is stored — validation is an exact-match index
probe on the hash, the same discipline as sessions.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.sessions import generate_service_token, hash_token
from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import ServiceToken, User
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.service_tokens import (
    AuthenticatedServiceToken,
    IssuedServiceToken,
    ServiceTokenRejection,
    ServiceTokenScope,
    ServiceTokenValidation,
)

MIN_SERVICE_TOKEN_TTL = timedelta(seconds=1)


class ServiceTokenRepository:
    """Issue and validate backend-issued opaque service tokens."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def issue(
        self,
        *,
        token_id: str,
        user_id: str,
        name: str,
        now: datetime,
        scope: ServiceTokenScope = ServiceTokenScope.READ,
        ttl: timedelta | None = None,
    ) -> IssuedServiceToken:
        """Create a service token, returning its secret **once**.

        ``ttl`` is optional on purpose: a personal agent's credential is
        long-lived and its lifecycle is revocation, not expiry.

        ``scope`` defaults to :attr:`ServiceTokenScope.READ`. The wider
        ``READ_SYNC`` is never the default: a caller that forgets to pass one
        must get the *narrower* grant, so an omission cannot hand out sync
        authority by accident.
        """
        if not token_id or not user_id or not name:
            msg = "token_id, user_id, and name must be non-empty"
            raise ValueError(msg)
        if ttl is not None and ttl < MIN_SERVICE_TOKEN_TTL:
            msg = "ttl must be at least one second (second-resolution timestamps)"
            raise ValueError(msg)

        now_aware = require_aware(now, field_name="now")
        created_at = to_utc_rfc3339(now_aware)
        expires_at = None if ttl is None else to_utc_rfc3339(now_aware + ttl)
        token = generate_service_token()

        with self._session_factory() as session, session.begin():
            tenant_id = session.execute(
                select(User.tenant_id).where(User.id == user_id)
            ).scalar_one_or_none()
            if tenant_id is None:
                msg = f"user {user_id!r} not found"
                raise ValueError(msg)

            session.add(
                ServiceToken(
                    id=token_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    name=name,
                    scope=scope.value,
                    token_hash=hash_token(token),
                    created_at=created_at,
                    expires_at=expires_at,
                    revoked_at=None,
                )
            )

        return IssuedServiceToken(
            token_id=token_id,
            tenant_id=tenant_id,
            user_id=user_id,
            name=name,
            scope=scope,
            token=token,
            expires_at=expires_at,
        )

    def validate(self, *, token: str, now: datetime) -> ServiceTokenValidation:
        """Validate a presented bearer token.

        Returns a typed rejection rather than raising, so callers surface one
        generic ``401`` without revealing which check failed.
        """
        if not token:
            return ServiceTokenValidation(rejection=ServiceTokenRejection.NOT_FOUND)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session:
            row = session.execute(
                select(
                    ServiceToken.id,
                    ServiceToken.tenant_id,
                    ServiceToken.user_id,
                    ServiceToken.scope,
                    ServiceToken.expires_at,
                    ServiceToken.revoked_at,
                ).where(ServiceToken.token_hash == hash_token(token))
            ).one_or_none()
            if row is None:
                return ServiceTokenValidation(rejection=ServiceTokenRejection.NOT_FOUND)

            token_id, tenant_id, user_id, scope, expires_at, revoked_at = row
            if revoked_at is not None:
                return ServiceTokenValidation(rejection=ServiceTokenRejection.REVOKED)
            if expires_at is not None and expires_at <= now_s:
                return ServiceTokenValidation(rejection=ServiceTokenRejection.EXPIRED)

            return ServiceTokenValidation(
                principal=AuthenticatedServiceToken(
                    token_id=token_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    scope=ServiceTokenScope(scope),
                    expires_at=expires_at,
                )
            )

    def revoke(self, *, token_id: str, now: datetime) -> bool:
        """Revoke one token by id. True when a live token was revoked."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(ServiceToken)
                .where(ServiceToken.id == token_id, ServiceToken.revoked_at.is_(None))
                .values(revoked_at=now_s)
            )
            return affected_rows(result) > 0

    def list_for_tenant(self, *, tenant_id: str) -> list[tuple[str, str, str, str | None]]:
        """List ``(id, name, scope, revoked_at)`` rows for operator inspection."""
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    ServiceToken.id,
                    ServiceToken.name,
                    ServiceToken.scope,
                    ServiceToken.revoked_at,
                )
                .where(ServiceToken.tenant_id == tenant_id)
                .order_by(ServiceToken.created_at)
            ).all()
            return [(r[0], r[1], r[2], r[3]) for r in rows]
