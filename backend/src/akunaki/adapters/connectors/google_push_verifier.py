"""Google push-webhook token verifier (signature + claims).

Google Health delivers webhooks as Google-signed OIDC tokens (Pub/Sub push
authentication). This verifier owns the **network + signature** half: it fetches
Google's rotating JWKS public keys (via PyJWT's ``PyJWKClient``, which caches and
rotates keys) and verifies the Bearer JWT's signature, then delegates the
**claim policy** to the pure ``validate_google_push_claims`` domain function.

Only asymmetric algorithms are accepted, closing the alg-confusion downgrade
class. Verification failures are a single boolean — the route surfaces one
generic rejection and never discloses which check failed.
"""

from __future__ import annotations

import logging
from datetime import datetime

import jwt
from jwt import PyJWKClient

from akunaki.domain.google_push import validate_google_push_claims

logger = logging.getLogger("akunaki.connectors.google_push")

# Google's public JWKS for the tokens minted by its OIDC token service.
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"

# Asymmetric signatures only. HS256 would let a shared secret forge a token;
# refusing it closes the RS256->HS256 alg-confusion downgrade.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384")


class GooglePushVerifier:
    """Verify a Google push OIDC token's signature and claims."""

    def __init__(
        self,
        *,
        expected_audience: str,
        expected_service_account: str,
        jwk_client: PyJWKClient | None = None,
    ) -> None:
        if not expected_audience.strip() or not expected_service_account.strip():
            msg = "expected_audience and expected_service_account must be non-empty"
            raise ValueError(msg)
        self._expected_audience = expected_audience
        self._expected_service_account = expected_service_account
        self._jwk_client = jwk_client

    def verify(self, *, bearer_token: str, now: datetime) -> bool:
        """Return True when the token's signature and claims are both valid.

        ``bearer_token`` is the raw JWT (no ``Bearer `` prefix). A bad signature,
        unknown key, disallowed algorithm, or failing claim all return False.
        """
        if not bearer_token:
            return False
        jwk_client = self._jwk_client or PyJWKClient(GOOGLE_JWKS_URI)
        try:
            signing_key = jwk_client.get_signing_key_from_jwt(bearer_token)
            # PyJWT verifies the signature; the domain validator owns every claim
            # policy (iss/aud/exp/email) against an injected clock, so PyJWT's
            # real-time exp/aud checks are turned off to keep one authority.
            claims = jwt.decode(
                bearer_token,
                signing_key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
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
            logger.warning("google push token signature verification failed")
            return False

        result = validate_google_push_claims(
            claims,
            expected_audience=self._expected_audience,
            expected_service_account=self._expected_service_account,
            now=now,
        )
        if not result.ok:
            logger.warning(
                "google push token claim validation failed",
                extra={"reason": str(result.rejection)},
            )
        return result.ok
