"""OIDC client: discovery, authorize URL, token exchange, id_token verification.

Signature verification uses PyJWT against the issuer's JWKS. Claim validation
(``iss``/``aud``/``nonce``/``exp``/``sub``) stays in ``akunaki.domain.oidc`` so
it is pure and reusable; this adapter is responsible only for the network and
cryptographic-signature parts PyJWT owns.

Secrets discipline matches the connector clients: the client secret and any
tokens are never logged, and provider response bodies are never attached to log
records or exceptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import httpx2
import jwt
from jwt import PyJWKClient

from akunaki.domain.oidc import (
    TokenRejection,
    TokenValidation,
    validate_id_token_claims,
)

logger = logging.getLogger("akunaki.oidc")

DEFAULT_TIMEOUT_SECONDS = 15.0
# Only asymmetric signatures are accepted. HS256 would let anyone holding the
# client secret forge an id_token; refusing it here closes the alg-confusion
# class where an attacker downgrades RS256 to HS256.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384")


@dataclass(frozen=True, slots=True)
class OIDCProviderMetadata:
    """The subset of discovery metadata this client uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


class OIDCConfigError(Exception):
    """Discovery metadata was missing or malformed."""


class OIDCExchangeError(Exception):
    """The token exchange failed. Carries no provider body."""


class OIDCClient:
    """Authorization-code + PKCE OIDC client for one issuer/client pair."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        transport: httpx2.Client | None = None,
        jwk_client: PyJWKClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        for name, value in (
            ("issuer", issuer),
            ("client_id", client_id),
            ("client_secret", client_secret),
        ):
            if not value.strip():
                msg = f"{name} must be a non-empty string"
                raise ValueError(msg)
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._timeout = timeout_seconds
        self._jwk_client = jwk_client
        self._metadata: OIDCProviderMetadata | None = None

    @property
    def issuer(self) -> str:
        """Configured issuer."""
        return self._issuer

    def __repr__(self) -> str:
        """Redacted: the client secret must never surface in logs."""
        return f"OIDCClient(issuer={self._issuer!r}, client_id=<redacted>)"

    def discover(self) -> OIDCProviderMetadata:
        """Fetch and cache the issuer's discovery metadata."""
        if self._metadata is not None:
            return self._metadata

        url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            response = self._get(url)
        except httpx2.HTTPError as exc:
            msg = "discovery request failed"
            raise OIDCConfigError(msg) from exc
        if response.status_code != 200:
            msg = f"discovery returned status {response.status_code}"
            raise OIDCConfigError(msg)
        try:
            body = response.json()
        except ValueError as exc:
            msg = "discovery response was not valid json"
            raise OIDCConfigError(msg) from exc

        metadata = _parse_metadata(body, expected_issuer=self._issuer)
        self._metadata = metadata
        return metadata

    def authorize_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: tuple[str, ...] = ("openid", "email"),
    ) -> str:
        """Build the authorize URL for a PKCE login."""
        for name, value in (
            ("state", state),
            ("nonce", nonce),
            ("code_challenge", code_challenge),
            ("redirect_uri", redirect_uri),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)
        if "openid" not in scopes:
            msg = "scopes must include 'openid'"
            raise ValueError(msg)

        metadata = self.discover()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata.authorization_endpoint}?{query}"

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        expected_nonce: str,
        now: datetime,
    ) -> TokenValidation:
        """Exchange an authorization code and validate the returned id_token.

        Returns a :class:`TokenValidation`: a verified identity on success, or
        a typed rejection. Raises :class:`OIDCExchangeError` only for transport
        or protocol failures that are not a claim rejection.
        """
        if not code or not code_verifier or not redirect_uri:
            msg = "code, code_verifier, and redirect_uri must be non-empty"
            raise ValueError(msg)

        metadata = self.discover()
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = self._post(metadata.token_endpoint, payload)
        except httpx2.HTTPError as exc:
            logger.warning("oidc token request transport error")
            msg = "token request failed"
            raise OIDCExchangeError(msg) from exc

        if response.status_code >= 400:
            logger.warning(
                "oidc token request rejected",
                extra={"status": response.status_code},
            )
            msg = "token endpoint rejected the exchange"
            raise OIDCExchangeError(msg)

        try:
            body = response.json()
        except ValueError as exc:
            msg = "token response was not valid json"
            raise OIDCExchangeError(msg) from exc

        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(id_token, str) or not id_token:
            msg = "token response contained no id_token"
            raise OIDCExchangeError(msg)

        return self._verify_id_token(
            id_token,
            metadata=metadata,
            expected_nonce=expected_nonce,
            now=now,
        )

    def _verify_id_token(
        self,
        id_token: str,
        *,
        metadata: OIDCProviderMetadata,
        expected_nonce: str,
        now: datetime,
    ) -> TokenValidation:
        """Verify the id_token signature, then validate its claims."""
        jwk_client = self._jwk_client or PyJWKClient(metadata.jwks_uri)
        try:
            signing_key = jwk_client.get_signing_key_from_jwt(id_token)
            # PyJWT verifies the signature and structural claims here; the
            # domain layer re-checks iss/aud/nonce/exp/sub with our own policy.
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                # PyJWT verifies the signature; the domain validator owns every
                # claim policy (iss/aud/nonce/exp/nbf/iat/sub) against an
                # injected clock, so PyJWT's real-time exp/nbf checks are turned
                # off to keep one authority over time and testable determinism.
                options={
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.InvalidTokenError:
            # A bad signature, unknown kid, or disallowed alg all land here.
            logger.warning("oidc id_token signature verification failed")
            return TokenValidation(rejection=TokenRejection.MALFORMED)

        return validate_id_token_claims(
            claims,
            expected_issuer=metadata.issuer,
            expected_audience=self._client_id,
            expected_nonce=expected_nonce,
            now=now,
        )

    def _get(self, url: str) -> httpx2.Response:
        if self._transport is not None:
            return self._transport.get(url, timeout=self._timeout)
        with httpx2.Client(timeout=self._timeout) as client:
            return client.get(url)

    def _post(self, url: str, payload: dict[str, str]) -> httpx2.Response:
        headers = {"Accept": "application/json"}
        if self._transport is not None:
            return self._transport.post(url, data=payload, headers=headers, timeout=self._timeout)
        with httpx2.Client(timeout=self._timeout) as client:
            return client.post(url, data=payload, headers=headers)


def _parse_metadata(body: object, *, expected_issuer: str) -> OIDCProviderMetadata:
    """Validate discovery metadata and confirm the issuer matches."""
    if not isinstance(body, dict):
        msg = "discovery metadata is not an object"
        raise OIDCConfigError(msg)

    issuer = body.get("issuer")
    if not isinstance(issuer, str) or issuer.rstrip("/") != expected_issuer:
        # A mismatched issuer means we are not talking to the provider we
        # configured; refuse rather than trust its endpoints.
        msg = "discovery issuer does not match the configured issuer"
        raise OIDCConfigError(msg)

    fields: dict[str, str] = {}
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = body.get(key)
        if not isinstance(value, str) or not value:
            msg = f"discovery metadata missing {key}"
            raise OIDCConfigError(msg)
        fields[key] = value

    return OIDCProviderMetadata(
        issuer=expected_issuer,
        authorization_endpoint=fields["authorization_endpoint"],
        token_endpoint=fields["token_endpoint"],
        jwks_uri=fields["jwks_uri"],
    )
