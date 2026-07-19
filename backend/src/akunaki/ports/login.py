"""Ports for OIDC login orchestration.

Adapters implement these protocols. The application layer depends only on
domain and ports, never on SQLAlchemy, an HTTP client, or crypto libraries.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from akunaki.domain.oidc import TokenValidation, VerifiedIdentity
from akunaki.domain.secrets import SealedSecret


class LoginStateConsumptionLike(Protocol):
    """The result of consuming a login state."""

    @property
    def state_id(self) -> str | None: ...

    @property
    def sealed_verifier(self) -> SealedSecret | None: ...

    @property
    def nonce_hash(self) -> str | None: ...

    @property
    def ok(self) -> bool: ...


class LoginStateStore(Protocol):
    """Create and single-use consume OIDC login states."""

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
    ) -> str: ...

    def consume(
        self, *, state: str, redirect_uri: str, now: datetime
    ) -> LoginStateConsumptionLike: ...


class OIDCClientPort(Protocol):
    """Build authorize URLs and exchange codes with signature verification."""

    def authorize_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str: ...

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        expected_nonce_hash: str,
        now: datetime,
    ) -> TokenValidation: ...


class ProvisionedUserLike(Protocol):
    """A user after login provisioning."""

    @property
    def user_id(self) -> str: ...

    @property
    def tenant_id(self) -> str: ...

    @property
    def created(self) -> bool: ...


class UserProvisioner(Protocol):
    """Provision or look up a user from a verified identity."""

    def upsert_from_identity(
        self,
        *,
        identity: VerifiedIdentity,
        user_id: str,
        tenant_id: str,
        now: datetime,
    ) -> ProvisionedUserLike: ...


class IssuedSessionLike(Protocol):
    """A newly issued session, secrets shown once."""

    @property
    def token(self) -> str: ...

    @property
    def csrf_secret(self) -> str: ...

    @property
    def expires_at(self) -> str: ...


class SessionIssuer(Protocol):
    """Issue backend sessions."""

    def issue(self, *, session_id: str, user_id: str, now: datetime) -> IssuedSessionLike: ...


class SecretSealerPort(Protocol):
    """Envelope-seal and open secret material."""

    def seal(self, plaintext: bytes, *, aad: bytes | None = None) -> SealedSecret: ...

    def open(self, sealed: SealedSecret, *, aad: bytes | None = None) -> bytes: ...
