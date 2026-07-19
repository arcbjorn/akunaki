"""OIDC login state: single-use, expiring authorize rows.

Mirrors ``OAuthStateRepository``: the raw ``state`` and ``nonce`` are never
stored, consumption is an atomic conditional UPDATE so a replayed callback
cannot win twice, and the sealed PKCE verifier is released only after every
check passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.oauth import hash_state, redirect_uri_matches
from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import LoginState
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.oauth import OAuthStateRejection
from akunaki.domain.secrets import SealedSecret

MIN_STATE_TTL = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class LoginStateConsumption:
    """Result of consuming a login state.

    Carries the sealed PKCE verifier and the **hashed** nonce; the raw nonce is
    never stored, so the caller compares the ``id_token``'s nonce claim by hash.
    """

    state_id: str | None = None
    sealed_verifier: SealedSecret | None = None
    nonce_hash: str | None = None
    redirect_uri: str | None = None
    rejection: OAuthStateRejection | None = None

    @property
    def ok(self) -> bool:
        """True when the state was validly consumed."""
        return self.rejection is None and self.sealed_verifier is not None


class LoginStateRepository:
    """Create and atomically consume OIDC login states."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        state_id: str,
        state: str,
        nonce: str,
        sealed_verifier: SealedSecret,
        redirect_uri: str,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        """Persist one login attempt. Returns the stored ``state_hash``."""
        for name, value in (
            ("state_id", state_id),
            ("state", state),
            ("nonce", nonce),
            ("redirect_uri", redirect_uri),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)
        if ttl < MIN_STATE_TTL:
            msg = "ttl must be at least one second (second-resolution timestamps)"
            raise ValueError(msg)

        now_aware = require_aware(now, field_name="now")
        state_hash = hash_state(state)

        with self._session_factory() as session, session.begin():
            session.add(
                LoginState(
                    id=state_id,
                    state_hash=state_hash,
                    nonce_hash=hash_state(nonce),
                    code_verifier_ciphertext=sealed_verifier.ciphertext,
                    code_verifier_key_version=sealed_verifier.key_version,
                    redirect_uri=redirect_uri,
                    created_at=to_utc_rfc3339(now_aware),
                    expires_at=to_utc_rfc3339(now_aware + ttl),
                    consumed_at=None,
                )
            )
        return state_hash

    def consume(
        self,
        *,
        state: str,
        redirect_uri: str,
        now: datetime,
    ) -> LoginStateConsumption:
        """Validate and single-use consume a login state."""
        if not state or not redirect_uri:
            return LoginStateConsumption(rejection=OAuthStateRejection.NOT_FOUND)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        state_hash = hash_state(state)

        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(
                    LoginState.id,
                    LoginState.code_verifier_ciphertext,
                    LoginState.code_verifier_key_version,
                    LoginState.nonce_hash,
                    LoginState.redirect_uri,
                    LoginState.expires_at,
                    LoginState.consumed_at,
                ).where(LoginState.state_hash == state_hash)
            ).one_or_none()
            if row is None:
                return LoginStateConsumption(rejection=OAuthStateRejection.NOT_FOUND)

            (
                state_id,
                ciphertext,
                key_version,
                nonce_hash,
                stored_redirect,
                expires_at,
                consumed_at,
            ) = row

            if consumed_at is not None:
                return LoginStateConsumption(rejection=OAuthStateRejection.ALREADY_CONSUMED)
            if expires_at <= now_s:
                return LoginStateConsumption(rejection=OAuthStateRejection.EXPIRED)
            if not redirect_uri_matches(redirect_uri, stored_redirect):
                return LoginStateConsumption(rejection=OAuthStateRejection.REDIRECT_MISMATCH)

            # Atomic single-use claim: only the first caller flips consumed_at
            # from NULL, so a concurrent replay loses even after reading the
            # same unconsumed row above.
            result = session.execute(
                update(LoginState)
                .where(
                    LoginState.id == state_id,
                    LoginState.consumed_at.is_(None),
                    LoginState.expires_at > now_s,
                )
                .values(consumed_at=now_s)
            )
            if affected_rows(result) != 1:
                return LoginStateConsumption(rejection=OAuthStateRejection.ALREADY_CONSUMED)

            return LoginStateConsumption(
                state_id=state_id,
                sealed_verifier=SealedSecret(ciphertext=ciphertext, key_version=key_version),
                nonce_hash=nonce_hash,
                redirect_uri=stored_redirect,
            )

    def purge_expired(self, *, now: datetime) -> int:
        """Delete login states past their expiry. Returns rows removed."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            result = session.execute(delete(LoginState).where(LoginState.expires_at <= now_s))
            return affected_rows(result)
