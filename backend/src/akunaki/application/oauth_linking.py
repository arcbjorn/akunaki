"""Provider OAuth linking: start authorization, complete the callback.

Port-typed and framework-free: no FastAPI, no SQLAlchemy, no HTTP client. The
``tenant_id`` is a parameter, so an HTTP layer can supply it from an
authenticated session once auth exists without changing this service.

The service owns the *orchestration* rules the design requires:

- the PKCE verifier is sealed before it is persisted and only ever opened
  after the state passes every callback check;
- the connection row and its sealed tokens are written together, so an
  ``active`` connection always has usable token material;
- an ``invalid_grant`` refusal flips the connection to ``needs_reauth``
  instead of being retried.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from akunaki.domain.connections import ConnectionStatus, LinkedConnection, Provider
from akunaki.domain.secrets import SecretDecryptionError
from akunaki.domain.tokens import OAuthTokens, TokenExchangeFailure
from akunaki.ports.connections import ConnectionRepositoryPort, OAuthStateRepositoryPort
from akunaki.ports.oauth_client import OAuthClientPort
from akunaki.ports.secrets import SecretSealerPort

logger = logging.getLogger("akunaki.oauth_linking")

DEFAULT_STATE_TTL = timedelta(minutes=10)


class LinkRejection(StrEnum):
    """Why a link attempt failed.

    Callers should surface a single generic error to the user; the distinction
    is for server-side metrics and connection-status decisions.
    """

    INVALID_STATE = "invalid_state"
    """State missing, forged, replayed, expired, or redirect mismatch."""

    VERIFIER_UNREADABLE = "verifier_unreadable"
    """Sealed PKCE verifier could not be opened (key rotation gap, tampering)."""

    PROVIDER_REJECTED = "provider_rejected"
    """Provider refused the grant; re-authorization is required."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """Transient provider or transport failure; retrying may succeed."""

    @property
    def retryable(self) -> bool:
        """Whether the user retrying the same link could plausibly succeed."""
        return self is LinkRejection.PROVIDER_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class AuthorizeRedirect:
    """Where to send the user agent to authorize, plus the stored state id."""

    authorize_url: str
    state_id: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class LinkResult:
    """Outcome of completing an OAuth callback."""

    connection: LinkedConnection | None = None
    rejection: LinkRejection | None = None

    @property
    def ok(self) -> bool:
        """True when the connection was linked."""
        return self.rejection is None and self.connection is not None


class OAuthLinkingService:
    """Start and complete provider OAuth links."""

    def __init__(
        self,
        *,
        client: OAuthClientPort,
        states: OAuthStateRepositoryPort,
        connections: ConnectionRepositoryPort,
        sealer: SecretSealerPort,
        generate_state: Callable[[], str],
        generate_code_verifier: Callable[[], str],
        code_challenge: Callable[[str], str],
        new_id: Callable[[], str],
        state_ttl: timedelta = DEFAULT_STATE_TTL,
    ) -> None:
        self._client = client
        self._states = states
        self._connections = connections
        self._sealer = sealer
        self._generate_state = generate_state
        self._generate_code_verifier = generate_code_verifier
        self._code_challenge = code_challenge
        self._new_id = new_id
        self._state_ttl = state_ttl

    def start_link(
        self,
        *,
        tenant_id: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        now: datetime,
    ) -> AuthorizeRedirect:
        """Begin an authorization: persist sealed state, return the authorize URL."""
        if not tenant_id:
            msg = "tenant_id must be non-empty"
            raise ValueError(msg)

        state = self._generate_state()
        verifier = self._generate_code_verifier()
        challenge = self._code_challenge(verifier)
        state_id = self._new_id()

        # Bind the sealed verifier to its own state row, so a stolen envelope
        # cannot be replayed against a different authorization attempt.
        sealed = self._sealer.seal(verifier.encode(), aad=state_id.encode())
        pending = self._states.create(
            state_id=state_id,
            tenant_id=tenant_id,
            provider=self._client.provider,
            state=state,
            sealed_verifier=sealed,
            redirect_uri=redirect_uri,
            now=now,
            ttl=self._state_ttl,
        )

        url = self._client.authorize_url(
            state=state,
            code_challenge=challenge,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )
        logger.info(
            "oauth link started",
            extra={"provider": self._client.provider, "state_id": state_id},
        )
        return AuthorizeRedirect(
            authorize_url=url,
            state_id=state_id,
            expires_at=pending.expires_at,
        )

    def complete_link(
        self,
        *,
        state: str,
        code: str,
        redirect_uri: str,
        now: datetime,
        connection_id: str | None = None,
    ) -> LinkResult:
        """Finish a callback: validate state, exchange code, store sealed tokens."""
        if not code:
            # An absent code means the provider denied or the callback is
            # malformed; the state is deliberately left unconsumed.
            return LinkResult(rejection=LinkRejection.INVALID_STATE)

        consumption = self._states.consume(
            state=state,
            redirect_uri=redirect_uri,
            now=now,
        )
        if not consumption.ok:
            logger.warning(
                "oauth callback state rejected",
                extra={
                    "provider": self._client.provider,
                    # A fixed vocabulary, never provider or user text.
                    "reason": str(consumption.rejection),
                },
            )
            return LinkResult(rejection=LinkRejection.INVALID_STATE)

        assert consumption.sealed_verifier is not None
        assert consumption.state_id is not None
        try:
            verifier = self._sealer.open(
                consumption.sealed_verifier,
                aad=consumption.state_id.encode(),
            ).decode()
        except SecretDecryptionError:
            logger.error(
                "sealed pkce verifier could not be opened",
                extra={"provider": self._client.provider, "state_id": consumption.state_id},
            )
            return LinkResult(rejection=LinkRejection.VERIFIER_UNREADABLE)

        exchange = self._client.exchange_code(
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            now=now,
        )
        if not exchange.ok:
            return LinkResult(rejection=self._map_exchange_failure(exchange.failure))

        assert exchange.tokens is not None
        tokens = exchange.tokens
        assert consumption.tenant_id is not None

        # Seal the whole token set under the connection's identity.
        resolved_connection_id = connection_id or self._new_id()
        sealed_tokens = self._sealer.seal(
            _serialize_tokens(tokens),
            aad=resolved_connection_id.encode(),
        )
        connection = self._connections.link(
            connection_id=resolved_connection_id,
            tenant_id=consumption.tenant_id,
            provider=Provider(self._client.provider),
            sealed_secret=sealed_tokens,
            scopes=tokens.scopes,
            # None for a provider that discloses no user id (Oura); populated
            # for one that returns it in the token body (Polar's x_user_id).
            external_user_id=tokens.external_user_id,
            now=now,
        )
        logger.info(
            "oauth link completed",
            extra={
                "provider": self._client.provider,
                "connection_id": connection.connection_id,
            },
        )
        return LinkResult(connection=connection)

    def _map_exchange_failure(self, failure: TokenExchangeFailure | None) -> LinkRejection:
        """Translate a token-exchange failure into a link rejection."""
        if failure is None or failure.retryable:
            return LinkRejection.PROVIDER_UNAVAILABLE
        # invalid_grant / invalid_client are permanent: the user must re-authorize.
        return LinkRejection.PROVIDER_REJECTED

    def mark_needs_reauth(self, *, connection_id: str, now: datetime) -> bool:
        """Flip a connection to ``needs_reauth`` after a permanent refusal."""
        return self._connections.mark_status(
            connection_id=connection_id,
            status=ConnectionStatus.NEEDS_REAUTH,
            now=now,
            error_class="invalid_grant",
        )


def _serialize_tokens(tokens: OAuthTokens) -> bytes:
    """Serialize tokens for sealing. Never logged; opened only on use."""
    return json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "token_type": tokens.token_type,
        }
    ).encode()
