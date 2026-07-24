"""Provider OAuth client port: authorize URL and token exchange.

Adapters implement this protocol. Domain and ports must not import an HTTP
client, so swapping transports or providers is an adapter change only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from akunaki.domain.tokens import TokenExchangeResult


class OAuthClientPort(Protocol):
    """Build authorize URLs and exchange authorization codes for tokens.

    Providers differ on PKCE: Oura and Google Health use it, Polar does not.
    ``uses_pkce`` tells the linking service whether to generate and thread a
    verifier; a non-PKCE client accepts ``code_challenge``/``code_verifier`` as
    ``None`` and ignores them, so one uniform signature covers both.
    """

    @property
    def provider(self) -> str:
        """Provider identifier (``oura``, ``google_health``, ``polar``)."""
        ...

    @property
    def uses_pkce(self) -> bool:
        """Whether this provider's flow uses PKCE (a verifier + challenge)."""
        ...

    def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str | None,
        redirect_uri: str,
        scopes: tuple[str, ...],
    ) -> str:
        """Return the provider authorize URL.

        ``code_challenge`` is the S256 transform of the verifier for a PKCE
        provider (the verifier itself never leaves the server), or ``None`` for
        a non-PKCE provider.
        """
        ...

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str | None,
        redirect_uri: str,
        now: datetime,
    ) -> TokenExchangeResult:
        """Exchange an authorization code for tokens.

        ``code_verifier`` is the PKCE verifier for a PKCE provider, or ``None``
        for a non-PKCE provider.
        """
        ...

    def refresh(self, *, refresh_token: str, now: datetime) -> TokenExchangeResult:
        """Exchange a refresh token for a new access token."""
        ...
