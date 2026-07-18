"""OAuth state persistence: single-use, expiring authorize rows.

The security rules live here rather than in callers, so no call site can
forget one:

- the raw ``state`` is never stored, only its hash;
- the PKCE verifier is stored sealed and only returned on a valid consume;
- consumption is an **atomic** conditional UPDATE, so a replayed callback
  cannot win twice even under concurrency;
- expiry and exact redirect-URI match are enforced before the verifier is
  released.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Select, delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.oauth import hash_state, redirect_uri_matches
from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import OAuthState
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.oauth import (
    OAuthStateConsumption,
    OAuthStateRejection,
    PendingAuthorization,
)
from akunaki.domain.secrets import SealedSecret

MIN_STATE_TTL = timedelta(seconds=1)


class OAuthStateRepository:
    """Create and atomically consume OAuth authorize state rows."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        state_id: str,
        tenant_id: str,
        provider: str,
        state: str,
        sealed_verifier: SealedSecret,
        redirect_uri: str,
        now: datetime,
        ttl: timedelta,
    ) -> PendingAuthorization:
        """Persist one authorize attempt and return its stored identity.

        ``state`` is hashed on the way in and never written in the clear.
        """
        for name, value in (
            ("state_id", state_id),
            ("tenant_id", tenant_id),
            ("provider", provider),
            ("state", state),
            ("redirect_uri", redirect_uri),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)
        if ttl < MIN_STATE_TTL:
            msg = "ttl must be at least one second (second-resolution timestamps)"
            raise ValueError(msg)

        now_aware = require_aware(now, field_name="now")
        created_at = to_utc_rfc3339(now_aware)
        expires_at = to_utc_rfc3339(now_aware + ttl)
        state_hash = hash_state(state)

        with self._session_factory() as session, session.begin():
            session.add(
                OAuthState(
                    id=state_id,
                    tenant_id=tenant_id,
                    provider=provider,
                    state_hash=state_hash,
                    code_verifier_ciphertext=sealed_verifier.ciphertext,
                    code_verifier_key_version=sealed_verifier.key_version,
                    redirect_uri=redirect_uri,
                    created_at=created_at,
                    expires_at=expires_at,
                    consumed_at=None,
                )
            )

        return PendingAuthorization(
            state_id=state_id,
            tenant_id=tenant_id,
            provider=provider,
            state_hash=state_hash,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
        )

    def consume(
        self,
        *,
        state: str,
        redirect_uri: str,
        now: datetime,
    ) -> OAuthStateConsumption:
        """Validate and single-use consume an authorize state.

        Returns the sealed PKCE verifier only when the state exists, is
        unconsumed, is unexpired, and the redirect URI matches exactly.
        Every failure returns a typed rejection rather than raising, so
        callers surface a generic error without leaking which check failed.
        """
        if not state or not redirect_uri:
            return OAuthStateConsumption(rejection=OAuthStateRejection.NOT_FOUND)

        now_aware = require_aware(now, field_name="now")
        now_s = to_utc_rfc3339(now_aware)
        state_hash = hash_state(state)

        with self._session_factory() as session, session.begin():
            row = session.execute(
                # Read the row first so specific rejections can be
                # distinguished internally; the claim itself is the atomic
                # UPDATE below.
                _select_by_hash(state_hash)
            ).one_or_none()
            if row is None:
                return OAuthStateConsumption(rejection=OAuthStateRejection.NOT_FOUND)

            (
                state_id,
                tenant_id,
                provider,
                ciphertext,
                key_version,
                stored_redirect,
                expires_at,
                consumed_at,
            ) = row

            if consumed_at is not None:
                return OAuthStateConsumption(rejection=OAuthStateRejection.ALREADY_CONSUMED)
            if expires_at <= now_s:
                return OAuthStateConsumption(rejection=OAuthStateRejection.EXPIRED)
            if not redirect_uri_matches(redirect_uri, stored_redirect):
                return OAuthStateConsumption(rejection=OAuthStateRejection.REDIRECT_MISMATCH)

            # Atomic single-use claim: only the first caller flips consumed_at
            # from NULL, so a concurrent replay loses even if it read the same
            # unconsumed row above.
            result = session.execute(
                update(OAuthState)
                .where(
                    OAuthState.id == state_id,
                    OAuthState.consumed_at.is_(None),
                    OAuthState.expires_at > now_s,
                )
                .values(consumed_at=now_s)
            )
            if affected_rows(result) != 1:
                return OAuthStateConsumption(rejection=OAuthStateRejection.ALREADY_CONSUMED)

            return OAuthStateConsumption(
                state_id=state_id,
                tenant_id=tenant_id,
                provider=provider,
                sealed_verifier=SealedSecret(ciphertext=ciphertext, key_version=key_version),
                redirect_uri=stored_redirect,
            )

    def purge_expired(self, *, now: datetime) -> int:
        """Delete states past their expiry. Returns rows removed.

        Consumed rows are also expiring rows, so a single sweep clears both
        rather than retaining spent verifiers indefinitely.
        """
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            result = session.execute(delete(OAuthState).where(OAuthState.expires_at <= now_s))
            return affected_rows(result)


def _select_by_hash(
    state_hash: str,
) -> Select[tuple[str, str, str, bytes, str, str, str, str | None]]:
    return select(
        OAuthState.id,
        OAuthState.tenant_id,
        OAuthState.provider,
        OAuthState.code_verifier_ciphertext,
        OAuthState.code_verifier_key_version,
        OAuthState.redirect_uri,
        OAuthState.expires_at,
        OAuthState.consumed_at,
    ).where(OAuthState.state_hash == state_hash)
