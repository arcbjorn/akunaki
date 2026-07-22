"""Polar AccessLink OAuth2 client: authorize URL and token exchange.

Implements the authorization-code flow against Polar's AccessLink endpoints.
Polar differs from Oura in three ways this client encodes:

- the token request authenticates with **HTTP Basic** (``client_id`` /
  ``client_secret``), not a client-secret form field;
- there is **no PKCE** and **no refresh token** — an AccessLink access token
  is long-lived, so there is no ``refresh`` operation;
- the token response carries an ``x_user_id`` (the vendor user id), captured as
  the connection's ``external_user_id``.

Secrets discipline matches the Oura client: the client secret is held only in
memory and never logged or returned; provider response bodies never reach
exceptions or log records; failures map to the shared typed vocabulary.
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

logger = logging.getLogger("akunaki.connectors.polar")

PROVIDER = "polar"
AUTHORIZE_ENDPOINT = "https://flow.polar.com/oauth2/authorization"
# Public endpoint URL, not a credential (S105 matches the "token" substring).
TOKEN_ENDPOINT = "https://polarremote.com/v2/oauth2/token"  # noqa: S105

DEFAULT_TIMEOUT_SECONDS = 15.0

# Provider error codes that mean "this grant will never work again", so the
# connection needs re-authorization rather than a retry.
_PERMANENT_ERROR_CODES = {
    "invalid_grant": TokenExchangeFailure.INVALID_GRANT,
    "invalid_client": TokenExchangeFailure.INVALID_CLIENT,
    "unauthorized_client": TokenExchangeFailure.INVALID_CLIENT,
}


class PolarOAuthClient:
    """Polar AccessLink authorization-code OAuth client (Basic-auth token)."""

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

    def __repr__(self) -> str:
        """Redacted repr: the client secret must never surface in logs."""
        return f"PolarOAuthClient(provider={PROVIDER!r}, client_id=<redacted>)"

    def authorize_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        scopes: tuple[str, ...] = ("accesslink.read_all",),
    ) -> str:
        """Return the Polar authorize URL.

        Polar does not use PKCE, so there is no ``code_challenge``; ``state`` is
        the CSRF binding. The default scope is the read-all AccessLink scope.
        """
        for name, value in (("state", state), ("redirect_uri", redirect_uri)):
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
            }
        )
        return f"{self._authorize_endpoint}?{query}"

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        now: datetime,
    ) -> TokenExchangeResult:
        """Exchange an authorization code for a long-lived access token.

        No PKCE verifier: Polar authenticates the exchange with Basic auth.
        """
        if not code or not redirect_uri:
            msg = "code and redirect_uri must be non-empty"
            raise ValueError(msg)
        return self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            now=now,
        )

    def _post_token(self, form: dict[str, str], *, now: datetime) -> TokenExchangeResult:
        """POST to the token endpoint with Basic auth; map to a typed result."""
        try:
            response = self._send(form)
        except httpx2.HTTPError:
            # Never attach the exception text: a transport error can carry the
            # request, whose Authorization header holds the client secret.
            logger.warning("polar token request transport error")
            return TokenExchangeResult(failure=TokenExchangeFailure.TRANSPORT_ERROR)

        if response.status_code >= 400:
            return TokenExchangeResult(failure=self._classify_error(response))

        try:
            body = response.json()
        except ValueError:
            logger.warning(
                "polar token response was not valid json",
                extra={"status": response.status_code},
            )
            return TokenExchangeResult(failure=TokenExchangeFailure.MALFORMED_RESPONSE)

        return self._parse_tokens(body, now=now)

    def _send(self, form: dict[str, str]) -> httpx2.Response:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        auth = httpx2.BasicAuth(self._client_id, self._client_secret)
        if self._transport is not None:
            return self._transport.post(
                self._token_endpoint,
                data=form,
                headers=headers,
                auth=auth,
                timeout=self._timeout,
            )
        with httpx2.Client(timeout=self._timeout) as client:
            return client.post(self._token_endpoint, data=form, headers=headers, auth=auth)

    def _classify_error(self, response: httpx2.Response) -> TokenExchangeFailure:
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
            "polar token request rejected",
            extra={
                "status": response.status_code,
                # The error *code* is a fixed vocabulary, not free-form text.
                "error_code": error_code or "unspecified",
                "failure": str(failure),
            },
        )
        return failure

    def _parse_tokens(self, body: object, *, now: datetime) -> TokenExchangeResult:
        if not isinstance(body, dict):
            return TokenExchangeResult(failure=TokenExchangeFailure.MALFORMED_RESPONSE)

        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.warning("polar token response missing access_token")
            return TokenExchangeResult(failure=TokenExchangeFailure.MALFORMED_RESPONSE)

        # Polar issues no refresh token; the access token is long-lived.
        expires_in = body.get("expires_in")
        expires_at = absolute_expiry(now, expires_in if isinstance(expires_in, int) else None)

        # The vendor user id arrives as x_user_id (int in the AccessLink body);
        # normalize to a string so it stores as the connection's external id.
        raw_user_id = body.get("x_user_id")
        external_user_id: str | None = None
        if isinstance(raw_user_id, bool):
            external_user_id = None
        elif isinstance(raw_user_id, int):
            external_user_id = str(raw_user_id)
        elif isinstance(raw_user_id, str) and raw_user_id:
            external_user_id = raw_user_id

        token_type = body.get("token_type")
        return TokenExchangeResult(
            tokens=OAuthTokens(
                access_token=access_token,
                refresh_token=None,
                expires_at=expires_at,
                scopes=(),
                token_type=token_type if isinstance(token_type, str) else "Bearer",
                external_user_id=external_user_id,
            )
        )
