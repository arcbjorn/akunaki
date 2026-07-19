"""OIDC client: discovery, token exchange, and id_token signature verification.

All traffic is served by an in-process mock transport; no test reaches a real
provider. Tokens are signed with a real RSA key so signature verification is
exercised end to end — a forged or wrong-key token must not authenticate, and
an HS256 token (the alg-confusion attack) must be refused.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient

from akunaki.adapters.crypto.oauth import hash_state
from akunaki.adapters.oidc.client import (
    OIDCClient,
    OIDCConfigError,
    OIDCExchangeError,
)
from akunaki.domain.oidc import TokenRejection

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
ISSUER = "https://auth.example.com"
CLIENT_ID = "akunaki-web"
CLIENT_SECRET = "oidc-client-SECRET"
REDIRECT = "https://app.example.com/auth/callback"
NONCE = "nonce-abc123"
KID = "test-key-1"

_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_WRONG_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict[str, object]:
    """A JWKS document containing this test's public key."""
    public_numbers = _SIGNING_KEY.public_key().public_numbers()

    def _b64(value: int) -> str:
        import base64

        length = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode().rstrip("=")

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": KID,
                "alg": "RS256",
                "n": _b64(public_numbers.n),
                "e": _b64(public_numbers.e),
            }
        ]
    }


def _sign(claims: dict[str, object], *, key: rsa.RSAPrivateKey = _SIGNING_KEY) -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def _discovery_body() -> dict[str, str]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }


def _claims(**overrides: object) -> dict[str, object]:
    epoch = int(NOW.timestamp())
    values: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "subject-1",
        "nonce": NONCE,
        "exp": epoch + 300,
        "iat": epoch - 5,
        "email": "person@example.com",
    }
    values.update(overrides)
    return values


def _transport(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.Client:
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _default_handler(id_token: str | None = None) -> Callable[[httpx2.Request], httpx2.Response]:
    """Serves discovery, JWKS, and a token response carrying ``id_token``."""
    token = id_token if id_token is not None else _sign(_claims())

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/openid-configuration"):
            return httpx2.Response(200, json=_discovery_body())
        if path.endswith("/jwks"):
            return httpx2.Response(200, json=_jwks())
        if path.endswith("/token"):
            return httpx2.Response(200, json={"id_token": token, "token_type": "Bearer"})
        return httpx2.Response(404)

    return handler


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> OIDCClient:
    transport = _transport(handler)
    return OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=transport,
        # A JWKS client bound to the same mock transport, so key fetch is faked.
        jwk_client=PyJWKClient(f"{ISSUER}/jwks"),
    )


class _StaticJWKClient(PyJWKClient):
    """PyJWKClient that returns this test's key without a network fetch."""

    def get_signing_key_from_jwt(self, token: str):  # type: ignore[no-untyped-def]
        from jwt import PyJWK

        [jwk] = _jwks()["keys"]  # type: ignore[index]
        return PyJWK.from_dict(jwk)


def _verifying_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> OIDCClient:
    return OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=_transport(handler),
        jwk_client=_StaticJWKClient(f"{ISSUER}/jwks"),
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovery_parses_endpoints() -> None:
    metadata = _client(_default_handler()).discover()

    assert metadata.issuer == ISSUER
    assert metadata.token_endpoint == f"{ISSUER}/token"
    assert metadata.jwks_uri == f"{ISSUER}/jwks"


def test_discovery_is_cached() -> None:
    calls = {"n": 0}

    def counting(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/openid-configuration"):
            calls["n"] += 1
        return _default_handler()(request)

    client = _client(counting)
    client.discover()
    client.discover()
    assert calls["n"] == 1


def test_issuer_mismatch_in_discovery_is_rejected() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx2.Response(200, json={**_discovery_body(), "issuer": "https://evil.test"})
        return _default_handler()(request)

    with pytest.raises(OIDCConfigError, match="issuer"):
        _client(handler).discover()


def test_missing_discovery_field_is_rejected() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/openid-configuration"):
            body = _discovery_body()
            del body["token_endpoint"]
            return httpx2.Response(200, json=body)
        return _default_handler()(request)

    with pytest.raises(OIDCConfigError, match="token_endpoint"):
        _client(handler).discover()


def test_discovery_http_error_is_rejected() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    with pytest.raises(OIDCConfigError):
        _client(handler).discover()


# ---------------------------------------------------------------------------
# Authorize URL
# ---------------------------------------------------------------------------


def test_authorize_url_carries_pkce_and_nonce() -> None:
    from urllib.parse import parse_qs, urlparse

    url = _client(_default_handler()).authorize_url(
        state="state-1",
        nonce=NONCE,
        code_challenge="challenge-1",
        redirect_uri=REDIRECT,
    )
    params = parse_qs(urlparse(url).query)

    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["nonce"] == [NONCE]
    assert params["state"] == ["state-1"]
    assert "openid" in params["scope"][0]


def test_authorize_url_requires_openid_scope() -> None:
    with pytest.raises(ValueError, match="openid"):
        _client(_default_handler()).authorize_url(
            state="s",
            nonce="n",
            code_challenge="c",
            redirect_uri=REDIRECT,
            scopes=("email",),
        )


# ---------------------------------------------------------------------------
# Token exchange and signature verification
# ---------------------------------------------------------------------------


def test_valid_exchange_yields_a_verified_identity() -> None:
    result = _verifying_client(_default_handler()).exchange_code(
        code="auth-code",
        code_verifier="v" * 64,
        redirect_uri=REDIRECT,
        expected_nonce_hash=hash_state(NONCE),
        now=NOW,
    )

    assert result.ok
    assert result.identity is not None
    assert result.identity.subject == "subject-1"
    assert result.identity.email == "person@example.com"


def test_token_signed_with_the_wrong_key_is_rejected() -> None:
    """A forged token whose signature does not match the JWKS must fail."""
    forged = _sign(_claims(), key=_WRONG_KEY)
    result = _verifying_client(_default_handler(forged)).exchange_code(
        code="c",
        code_verifier="v" * 64,
        redirect_uri=REDIRECT,
        expected_nonce_hash=hash_state(NONCE),
        now=NOW,
    )

    assert not result.ok
    assert result.rejection is TokenRejection.MALFORMED


def test_hs256_token_is_refused_alg_confusion() -> None:
    """An HS256 token forged with a symmetric key must not be accepted.

    The attack: an attacker who knows a public key uses it as an HMAC secret
    and signs HS256, hoping the verifier confuses the algorithms. The client
    only accepts asymmetric algorithms, so it must reject this. (A 32-byte key
    avoids PyJWT's short-key warning during test setup; the length is
    irrelevant to what is being tested.)
    """
    hs_token = jwt.encode(_claims(), "x" * 32, algorithm="HS256", headers={"kid": KID})
    result = _verifying_client(_default_handler(hs_token)).exchange_code(
        code="c",
        code_verifier="v" * 64,
        redirect_uri=REDIRECT,
        expected_nonce_hash=hash_state(NONCE),
        now=NOW,
    )

    assert result.rejection is TokenRejection.MALFORMED


def test_wrong_nonce_is_rejected_after_signature_check() -> None:
    """A validly-signed token from a different login must not authenticate."""
    token = _sign(_claims(nonce="a-different-nonce"))
    result = _verifying_client(_default_handler(token)).exchange_code(
        code="c",
        code_verifier="v" * 64,
        redirect_uri=REDIRECT,
        expected_nonce_hash=hash_state(NONCE),
        now=NOW,
    )

    assert result.rejection is TokenRejection.NONCE_MISMATCH


def test_wrong_audience_is_rejected() -> None:
    token = _sign(_claims(aud="some-other-client"))
    result = _verifying_client(_default_handler(token)).exchange_code(
        code="c",
        code_verifier="v" * 64,
        redirect_uri=REDIRECT,
        expected_nonce_hash=hash_state(NONCE),
        now=NOW,
    )

    assert result.rejection is TokenRejection.AUDIENCE_MISMATCH


def test_token_endpoint_error_raises() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/token"):
            return httpx2.Response(400, json={"error": "invalid_grant"})
        return _default_handler()(request)

    with pytest.raises(OIDCExchangeError):
        _verifying_client(handler).exchange_code(
            code="c",
            code_verifier="v" * 64,
            redirect_uri=REDIRECT,
            expected_nonce_hash=hash_state(NONCE),
            now=NOW,
        )


def test_missing_id_token_raises() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/token"):
            return httpx2.Response(200, json={"token_type": "Bearer"})
        return _default_handler()(request)

    with pytest.raises(OIDCExchangeError, match="id_token"):
        _verifying_client(handler).exchange_code(
            code="c",
            code_verifier="v" * 64,
            redirect_uri=REDIRECT,
            expected_nonce_hash=hash_state(NONCE),
            now=NOW,
        )


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------


def test_client_secret_is_sent_but_not_in_repr() -> None:
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _default_handler()(request)

    client = _verifying_client(recording)
    client.exchange_code(
        code="c",
        code_verifier="v" * 64,
        redirect_uri=REDIRECT,
        expected_nonce_hash=hash_state(NONCE),
        now=NOW,
    )

    token_request = next(r for r in seen if r.url.path.endswith("/token"))
    from urllib.parse import parse_qs

    form = parse_qs(token_request.content.decode())
    assert form["client_secret"] == [CLIENT_SECRET]
    assert CLIENT_SECRET not in repr(client)


def test_error_logs_carry_no_token_body(caplog: pytest.LogCaptureFixture) -> None:
    """A token endpoint body can contain credentials; it must not be logged."""
    leaky = json.dumps({"error": "invalid_grant", "id_token": "leaked.jwt.here"})

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/token"):
            return httpx2.Response(400, content=leaky, headers={"content-type": "application/json"})
        return _default_handler()(request)

    import logging

    logger = logging.getLogger("akunaki.oidc")
    records: list[logging.LogRecord] = []
    handler_obj = logging.Handler()
    handler_obj.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler_obj)
    try:
        with pytest.raises(OIDCExchangeError):
            _verifying_client(handler).exchange_code(
                code="c",
                code_verifier="v" * 64,
                redirect_uri=REDIRECT,
                expected_nonce_hash=hash_state(NONCE),
                now=NOW,
            )
    finally:
        logger.removeHandler(handler_obj)

    rendered = "\n".join(
        [r.getMessage() for r in records] + [json.dumps(r.__dict__, default=str) for r in records]
    )
    assert records, "the rejection path did not log"
    assert "leaked.jwt.here" not in rendered


def test_construction_requires_credentials() -> None:
    with pytest.raises(ValueError, match="client_id must be"):
        OIDCClient(issuer=ISSUER, client_id="", client_secret=CLIENT_SECRET)
    with pytest.raises(ValueError, match="client_secret must be"):
        OIDCClient(issuer=ISSUER, client_id=CLIENT_ID, client_secret="  ")
