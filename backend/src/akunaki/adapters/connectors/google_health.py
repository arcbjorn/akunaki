"""Google Health OAuth2 client: authorize URL and PKCE token exchange.

Implements Google's authorization-code + PKCE flow against the standard Google
OAuth2 endpoints, used to authorize Google Health API v4 access. Google is a
confidential client: the ``client_secret`` rides in the token-request **form**
(like Oura, unlike Polar's Basic auth), and it issues a **refresh token** on
first consent.

Two Google-specific authorize parameters matter: ``access_type=offline`` and
``prompt=consent`` are required to reliably receive a refresh token, without
which a long-lived connection cannot refresh its access.

Secrets discipline matches the Oura and Polar clients: the client secret is held
only in memory and never logged or returned; provider response bodies never
reach exceptions or log records; failures map to the shared typed vocabulary.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlencode

import httpx2

from akunaki.domain.tokens import (
    OAuthTokens,
    TokenExchangeFailure,
    TokenExchangeResult,
    absolute_expiry,
)

logger = logging.getLogger("akunaki.connectors.google_health")

PROVIDER = "google_health"
AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
# Public endpoint URL, not a credential (S105 matches the "token" substring).
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105

DEFAULT_TIMEOUT_SECONDS = 15.0

# Provider error codes that mean "this grant will never work again", so the
# connection needs re-authorization rather than a retry.
_PERMANENT_ERROR_CODES = {
    "invalid_grant": TokenExchangeFailure.INVALID_GRANT,
    "invalid_client": TokenExchangeFailure.INVALID_CLIENT,
    "unauthorized_client": TokenExchangeFailure.INVALID_CLIENT,
}


class GoogleHealthOAuthClient:
    """Google authorization-code + PKCE OAuth client for Google Health."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: httpx2.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        authorize_endpoint: str = AUTHORIZE_ENDPOINT,
        token_endpoint: str = TOKEN_ENDPOINT,
    ) -> None:
        if not client_id.strip():
            msg = "client_id must be a non-empty string"
            raise ValueError(msg)
        if not client_secret.strip():
            msg = "client_secret must be a non-empty string"
            raise ValueError(msg)
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._timeout = timeout_seconds
        self._authorize_endpoint = authorize_endpoint
        self._token_endpoint = token_endpoint

    @property
    def provider(self) -> str:
        """Provider identifier."""
        return PROVIDER

    @property
    def uses_pkce(self) -> bool:
        """Google Health uses the authorization-code + PKCE flow."""
        return True

    def __repr__(self) -> str:
        """Redacted repr: the client secret must never surface in logs."""
        return f"GoogleHealthOAuthClient(provider={PROVIDER!r}, client_id=<redacted>)"

    def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str | None,
        redirect_uri: str,
        scopes: tuple[str, ...],
    ) -> str:
        """Return the Google authorize URL for a PKCE flow.

        ``access_type=offline`` + ``prompt=consent`` are required so Google
        returns a refresh token; without them a connection could not refresh.
        """
        for name, value in (
            ("state", state),
            ("code_challenge", code_challenge),
            ("redirect_uri", redirect_uri),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)
        if not scopes:
            msg = "at least one scope is required"
            raise ValueError(msg)

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": code_challenge,
                # S256 only; `plain` offers no protection against a leaked code.
                "code_challenge_method": "S256",
                # Required for a refresh token on the standard Google flow.
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return f"{self._authorize_endpoint}?{query}"

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str | None,
        redirect_uri: str,
        now: datetime,
    ) -> TokenExchangeResult:
        """Exchange an authorization code plus PKCE verifier for tokens."""
        if not code or not code_verifier or not redirect_uri:
            msg = "code, code_verifier, and redirect_uri must be non-empty"
            raise ValueError(msg)
        return self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            now=now,
            operation="exchange_code",
        )

    def refresh(self, *, refresh_token: str, now: datetime) -> TokenExchangeResult:
        """Exchange a refresh token for a new access token."""
        if not refresh_token:
            msg = "refresh_token must be non-empty"
            raise ValueError(msg)
        return self._post_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            now=now,
            operation="refresh",
        )

    def _post_token(
        self,
        form: dict[str, str],
        *,
        now: datetime,
        operation: str,
    ) -> TokenExchangeResult:
        """POST to the token endpoint and map the outcome to a typed result."""
        payload = dict(form)
        payload["client_id"] = self._client_id
        payload["client_secret"] = self._client_secret

        try:
            response = self._send(payload)
        except httpx2.HTTPError:
            # Never attach the exception text: a transport error can carry the
            # request body, which holds the client secret and code verifier.
            logger.warning(
                "google_health token request transport error",
                extra={"operation": operation},
            )
            return TokenExchangeResult(failure=TokenExchangeFailure.TRANSPORT_ERROR)

        if response.status_code >= 400:
            return TokenExchangeResult(failure=self._classify_error(response, operation=operation))

        try:
            body = response.json()
        except ValueError:
            logger.warning(
                "google_health token response was not valid json",
                extra={"operation": operation, "status": response.status_code},
            )
            return TokenExchangeResult(failure=TokenExchangeFailure.MALFORMED_RESPONSE)

        return self._parse_tokens(body, now=now, operation=operation)

    def _send(self, payload: dict[str, str]) -> httpx2.Response:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self._transport is not None:
            return self._transport.post(
                self._token_endpoint,
                data=payload,
                headers=headers,
                timeout=self._timeout,
            )
        with httpx2.Client(timeout=self._timeout) as client:
            return client.post(self._token_endpoint, data=payload, headers=headers)

    def _classify_error(self, response: httpx2.Response, *, operation: str) -> TokenExchangeFailure:
        """Map a non-2xx token response to a typed failure.

        Only the provider's ``error`` **code** is inspected; the body is never
        logged, since a token endpoint response may contain credentials.
        """
        error_code = ""
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            raw = body.get("error")
            if isinstance(raw, str):
                error_code = raw.strip().lower()

        failure = _PERMANENT_ERROR_CODES.get(error_code)
        if failure is None:
            failure = (
                TokenExchangeFailure.PROVIDER_ERROR
                if response.status_code >= 500
                else TokenExchangeFailure.INVALID_GRANT
            )

        logger.warning(
            "google_health token request rejected",
            extra={
                "operation": operation,
                "status": response.status_code,
                # The error *code* is a fixed vocabulary, not free-form text.
                "error_code": error_code or "unspecified",
                "failure": str(failure),
            },
        )
        return failure

    def _parse_tokens(
        self,
        body: object,
        *,
        now: datetime,
        operation: str,
    ) -> TokenExchangeResult:
        if not isinstance(body, dict):
            return TokenExchangeResult(failure=TokenExchangeFailure.MALFORMED_RESPONSE)

        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.warning(
                "google_health token response missing access_token",
                extra={"operation": operation},
            )
            return TokenExchangeResult(failure=TokenExchangeFailure.MALFORMED_RESPONSE)

        # A refresh token is returned on first consent; a refresh response omits
        # it, keeping the stored one in force.
        refresh_token = body.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = None

        expires_in = body.get("expires_in")
        expires_at = absolute_expiry(now, expires_in if isinstance(expires_in, int) else None)

        raw_scope = body.get("scope")
        scopes = tuple(raw_scope.split()) if isinstance(raw_scope, str) and raw_scope else ()

        token_type = body.get("token_type")
        return TokenExchangeResult(
            tokens=OAuthTokens(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                scopes=scopes,
                token_type=token_type if isinstance(token_type, str) else "Bearer",
            )
        )
