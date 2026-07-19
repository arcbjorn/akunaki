"""Provider OAuth client port: authorize URL and token exchange.

Adapters implement this protocol. Domain and ports must not import an HTTP
client, so swapping transports or providers is an adapter change only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from akunaki.domain.tokens import TokenExchangeResult


class OAuthClientPort(Protocol):
    """Build authorize URLs and exchange authorization codes for tokens."""

    @property
    def provider(self) -> str:
        """Provider identifier (``oura``, ``google_health``, ``polar``)."""
        ...

    def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
    ) -> str:
        """Return the provider authorize URL for a PKCE flow.

        ``code_challenge`` is the S256 transform of the verifier; the verifier
        itself never leaves the server.
        """
        ...

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        now: datetime,
    ) -> TokenExchangeResult:
        """Exchange an authorization code plus PKCE verifier for tokens."""
        ...

    def refresh(self, *, refresh_token: str, now: datetime) -> TokenExchangeResult:
        """Exchange a refresh token for a new access token."""
        ...
