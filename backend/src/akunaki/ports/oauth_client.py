"""Provider OAuth client port: authorize URL and token exchange.

Adapters implement this protocol. Domain and ports must not import an HTTP
client, so swapping transports or providers is an adapter change only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from akunaki.domain.tokens import EnrollmentResult, TokenExchangeResult


class OAuthClientPort(Protocol):
    """Build authorize URLs and exchange authorization codes for tokens.

    Providers differ on PKCE: Oura and Google Health use it, Polar does not.
    ``uses_pkce`` tells the linking service whether to generate and thread a
    verifier; a non-PKCE client accepts ``code_challenge``/``code_verifier`` as
    ``None`` and ignores them, so one uniform signature covers both.

    They also differ on *enrollment*: Polar AccessLink serves no data until the
    authorized user is registered to the calling client, while Oura and Google
    Health need no such step. ``requires_enrollment`` tells the linking service
    whether to run it, so the service stays provider-uniform.
    """

    @property
    def provider(self) -> str:
        """Provider identifier (``oura``, ``google_health``, ``polar``)."""
        ...

    @property
    def uses_pkce(self) -> bool:
        """Whether this provider's flow uses PKCE (a verifier + challenge)."""
        ...

    @property
    def requires_enrollment(self) -> bool:
        """Whether a linked user must be enrolled before data can be fetched."""
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

    def enroll_user(
        self,
        *,
        access_token: str,
        external_user_id: str | None,
    ) -> EnrollmentResult:
        """Register the authorized user with the calling client.

        Called only when ``requires_enrollment`` is true. A user the client has
        already registered is a success (``already_enrolled``), so a re-consent
        does not fail the link.

        A provider that serves data on the grant alone declares
        ``requires_enrollment = False`` and returns an immediate success here.
        """
        ...
