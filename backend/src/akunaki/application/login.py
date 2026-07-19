"""OIDC login orchestration: begin a login, complete a callback.

Port-typed and framework-free. The two methods mirror the two HTTP legs but
own the security ordering so a route cannot get it wrong:

- **begin** seals the PKCE verifier and persists the login state *before*
  handing the authorize URL to the browser;
- **complete** consumes the state single-use, then exchanges the code and
  validates the ``id_token`` (signature + claims), and only on success
  provisions the user and issues a session.

Every failure collapses to one generic rejection: telling a caller which check
failed would help probe the flow.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from akunaki.domain.oidc import VerifiedIdentity
from akunaki.ports.login import (
    LoginStateStore,
    OIDCClientPort,
    SecretSealerPort,
    SessionIssuer,
    UserProvisioner,
)

logger = logging.getLogger("akunaki.login")

DEFAULT_STATE_TTL = timedelta(minutes=10)


class LoginRejection(StrEnum):
    """Why a login could not complete."""

    INVALID_STATE = "invalid_state"
    """State missing, forged, replayed, expired, or redirect mismatch."""

    VERIFIER_UNREADABLE = "verifier_unreadable"
    """Sealed PKCE verifier could not be opened (key rotation gap, tampering)."""

    TOKEN_REJECTED = "token_rejected"  # noqa: S105 - a rejection reason, not a credential
    """The provider's ``id_token`` failed signature or claim validation."""

    PROVIDER_ERROR = "provider_error"
    """Discovery or token exchange failed at the transport level."""


@dataclass(frozen=True, slots=True)
class LoginRedirect:
    """Where to send the user agent to authenticate."""

    authorize_url: str
    state_id: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of completing a login callback.

    On success ``issued_token`` and ``csrf_secret`` are the secrets shown once;
    the route sets the cookie and returns the CSRF secret, then drops them.
    """

    tenant_id: str | None = None
    user_id: str | None = None
    session_token: str | None = None
    csrf_secret: str | None = None
    session_expires_at: str | None = None
    rejection: LoginRejection | None = None

    @property
    def ok(self) -> bool:
        """True when a session was issued."""
        return self.rejection is None and self.session_token is not None


class LoginService:
    """Begin and complete OIDC logins."""

    def __init__(
        self,
        *,
        client: OIDCClientPort,
        states: LoginStateStore,
        users: UserProvisioner,
        sessions: SessionIssuer,
        sealer: SecretSealerPort,
        generate_state: Callable[[], str],
        generate_nonce: Callable[[], str],
        generate_code_verifier: Callable[[], str],
        code_challenge: Callable[[str], str],
        new_id: Callable[[], str],
        state_ttl: timedelta = DEFAULT_STATE_TTL,
    ) -> None:
        self._client = client
        self._states = states
        self._users = users
        self._sessions = sessions
        self._sealer = sealer
        self._generate_state = generate_state
        self._generate_nonce = generate_nonce
        self._generate_code_verifier = generate_code_verifier
        self._code_challenge = code_challenge
        self._new_id = new_id
        self._state_ttl = state_ttl

    def begin(self, *, redirect_uri: str, now: datetime) -> LoginRedirect:
        """Start a login: seal state, then return the authorize URL."""
        if not redirect_uri:
            msg = "redirect_uri must be non-empty"
            raise ValueError(msg)

        state = self._generate_state()
        nonce = self._generate_nonce()
        verifier = self._generate_code_verifier()
        challenge = self._code_challenge(verifier)
        state_id = self._new_id()

        # Bind the sealed verifier to its own state row, so a stolen envelope
        # cannot be replayed against a different login attempt.
        sealed = self._sealer.seal(verifier.encode(), aad=state_id.encode())
        self._states.create(
            state_id=state_id,
            state=state,
            nonce=nonce,
            sealed_verifier=sealed,
            redirect_uri=redirect_uri,
            now=now,
            ttl=self._state_ttl,
        )

        url = self._client.authorize_url(
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            redirect_uri=redirect_uri,
        )
        logger.info("login begun", extra={"state_id": state_id})
        return LoginRedirect(authorize_url=url, state_id=state_id)

    def complete(
        self,
        *,
        state: str,
        code: str,
        redirect_uri: str,
        now: datetime,
    ) -> LoginResult:
        """Complete a callback: validate state, verify token, issue a session."""
        if not code:
            # An absent code means the provider denied or the callback is
            # malformed; the state is deliberately left unconsumed.
            return LoginResult(rejection=LoginRejection.INVALID_STATE)

        consumption = self._states.consume(state=state, redirect_uri=redirect_uri, now=now)
        if not consumption.ok:
            logger.warning("login state rejected")
            return LoginResult(rejection=LoginRejection.INVALID_STATE)

        assert consumption.sealed_verifier is not None
        assert consumption.state_id is not None
        assert consumption.nonce_hash is not None
        try:
            verifier = self._sealer.open(
                consumption.sealed_verifier,
                aad=consumption.state_id.encode(),
            ).decode()
        except Exception:
            logger.error("sealed pkce verifier could not be opened")
            return LoginResult(rejection=LoginRejection.VERIFIER_UNREADABLE)

        try:
            validation = self._client.exchange_code(
                code=code,
                code_verifier=verifier,
                redirect_uri=redirect_uri,
                # The raw nonce is never stored; the client hashes the token's
                # nonce claim and compares it to this.
                expected_nonce_hash=consumption.nonce_hash,
                now=now,
            )
        except Exception:
            logger.warning("oidc token exchange failed")
            return LoginResult(rejection=LoginRejection.PROVIDER_ERROR)

        if not validation.ok or validation.identity is None:
            logger.warning(
                "id_token rejected",
                extra={"reason": str(validation.rejection)},
            )
            return LoginResult(rejection=LoginRejection.TOKEN_REJECTED)

        return self._establish_session(validation.identity, now=now)

    def _establish_session(self, identity: VerifiedIdentity, *, now: datetime) -> LoginResult:
        """Provision the user and issue a session for a verified identity."""
        provisioned = self._users.upsert_from_identity(
            identity=identity,
            user_id=self._new_id(),
            tenant_id=self._new_id(),
            now=now,
        )
        issued = self._sessions.issue(
            session_id=self._new_id(),
            user_id=provisioned.user_id,
            now=now,
        )
        logger.info(
            "login completed",
            extra={"user_id": provisioned.user_id, "new_user": provisioned.created},
        )
        return LoginResult(
            tenant_id=provisioned.tenant_id,
            user_id=provisioned.user_id,
            session_token=issued.token,
            csrf_secret=issued.csrf_secret,
            session_expires_at=issued.expires_at,
        )
