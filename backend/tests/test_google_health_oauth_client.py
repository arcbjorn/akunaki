"""Google Health OAuth client: authorize URL, PKCE exchange, failure mapping.

All network traffic is served by an in-process mock transport; these tests
never reach the real Google API. The security assertions are as important as the
happy path: no client secret, code verifier, or token value may appear in a log
record, exception, or repr. Google-specific behavior verified: the authorize URL
carries access_type=offline + prompt=consent (needed for a refresh token) and
the client_secret rides in the token form (not Basic auth).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from akunaki.adapters.connectors.google_health import PROVIDER, GoogleHealthOAuthClient
from akunaki.domain.tokens import TokenExchangeFailure

T0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
CLIENT_ID = "google-client-id"
CLIENT_SECRET = "google-client-SECRET-value"
REDIRECT = "https://app.example.com/oauth/google/callback"
CODE_VERIFIER = "v" * 64
ACCESS_TOKEN = "google-access-TOKEN-value"
REFRESH_TOKEN = "google-refresh-TOKEN-value"

TOKEN_BODY = {
    "access_token": ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
    "expires_in": 3600,
    "scope": "https://www.googleapis.com/auth/health.sleep.read",
    "token_type": "Bearer",
}

GOOGLE_LOGGER = "akunaki.connectors.google_health"


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[GoogleHealthOAuthClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    transport = httpx2.Client(transport=httpx2.MockTransport(recording))
    return (
        GoogleHealthOAuthClient(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            transport=transport,
        ),
        seen,
    )


def _json_response(status: int, body: object) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body)

    return handler


def _form_of(request: httpx2.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def rendered(self) -> str:
        return "\n".join(
            [record.getMessage() for record in self.records]
            + [json.dumps(record.__dict__, default=str) for record in self.records]
        )


@contextmanager
def _captured_logs() -> Iterator[_Capture]:
    logger = logging.getLogger(GOOGLE_LOGGER)
    handler = _Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# ---------------------------------------------------------------------------
# Authorize URL
# ---------------------------------------------------------------------------


def test_authorize_url_carries_pkce_offline_and_consent() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    url = client.authorize_url(
        state="state-abc",
        code_challenge="challenge-xyz",
        redirect_uri=REDIRECT,
        scopes=("https://www.googleapis.com/auth/health.sleep.read",),
    )

    parsed = urlparse(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert parsed.netloc == "accounts.google.com"
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT
    assert params["state"] == "state-abc"
    assert params["code_challenge"] == "challenge-xyz"
    assert params["code_challenge_method"] == "S256"
    # Google-specific: both are required to obtain a refresh token.
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"


def test_authorize_url_never_contains_the_client_secret() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    url = client.authorize_url(
        state="s", code_challenge="c", redirect_uri=REDIRECT, scopes=("scope",)
    )
    assert CLIENT_SECRET not in url


def test_authorize_url_validates_arguments() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    for field in ("state", "code_challenge", "redirect_uri"):
        kwargs = {
            "state": "s",
            "code_challenge": "c",
            "redirect_uri": REDIRECT,
            "scopes": ("scope",),
        }
        kwargs[field] = ""
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            client.authorize_url(**kwargs)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="at least one scope"):
        client.authorize_url(state="s", code_challenge="c", redirect_uri=REDIRECT, scopes=())


# ---------------------------------------------------------------------------
# Code exchange
# ---------------------------------------------------------------------------


def test_exchange_code_sends_secret_in_form_and_returns_tokens() -> None:
    client, seen = _client(_json_response(200, TOKEN_BODY))

    result = client.exchange_code(
        code="auth-code-1", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )

    assert result.ok
    assert result.tokens is not None
    assert result.tokens.access_token == ACCESS_TOKEN
    assert result.tokens.refresh_token == REFRESH_TOKEN
    # expires_in is converted to an absolute instant so it survives a restart.
    assert result.tokens.expires_at == "2026-07-23T13:00:00Z"

    form = _form_of(seen[0])
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code-1"
    assert form["code_verifier"] == CODE_VERIFIER
    assert form["redirect_uri"] == REDIRECT
    assert form["client_id"] == CLIENT_ID
    # Google is a confidential client: the secret is a form field, not Basic.
    assert form["client_secret"] == CLIENT_SECRET
    assert "authorization" not in {k.lower() for k in seen[0].headers}


def test_refresh_sends_refresh_grant_and_keeps_stored_refresh_token() -> None:
    # A refresh response omits refresh_token; the stored one stays in force.
    client, seen = _client(_json_response(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600}))

    result = client.refresh(refresh_token=REFRESH_TOKEN, now=T0)

    assert result.ok
    assert result.tokens is not None
    assert result.tokens.refresh_token is None
    form = _form_of(seen[0])
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == REFRESH_TOKEN


def test_missing_optional_fields_are_tolerated() -> None:
    client, _ = _client(_json_response(200, {"access_token": ACCESS_TOKEN}))

    result = client.exchange_code(
        code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )

    assert result.ok
    assert result.tokens is not None
    assert result.tokens.refresh_token is None
    assert result.tokens.expires_at is None
    assert result.tokens.scopes == ()
    assert result.tokens.token_type == "Bearer"


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("invalid_grant", TokenExchangeFailure.INVALID_GRANT),
        ("invalid_client", TokenExchangeFailure.INVALID_CLIENT),
        ("unauthorized_client", TokenExchangeFailure.INVALID_CLIENT),
    ],
)
def test_permanent_provider_errors_are_not_retryable(
    error_code: str, expected: TokenExchangeFailure
) -> None:
    client, _ = _client(_json_response(400, {"error": error_code}))

    result = client.exchange_code(
        code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )

    assert not result.ok
    assert result.failure is expected
    assert expected.retryable is False


def test_server_error_is_retryable() -> None:
    client, _ = _client(_json_response(503, {"error": "backend_error"}))
    result = client.exchange_code(
        code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )
    assert result.failure is TokenExchangeFailure.PROVIDER_ERROR
    assert result.failure.retryable is True


def test_transport_error_is_retryable() -> None:
    def boom(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    client, _ = _client(boom)
    result = client.exchange_code(
        code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )
    assert result.failure is TokenExchangeFailure.TRANSPORT_ERROR
    assert result.failure.retryable is True


def test_non_json_response_is_malformed() -> None:
    def html(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>gateway</html>")

    client, _ = _client(html)
    result = client.exchange_code(
        code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )
    assert result.failure is TokenExchangeFailure.MALFORMED_RESPONSE


def test_response_without_access_token_is_malformed() -> None:
    client, _ = _client(_json_response(200, {"token_type": "Bearer"}))
    result = client.exchange_code(
        code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )
    assert result.failure is TokenExchangeFailure.MALFORMED_RESPONSE
    assert result.tokens is None


def test_exchange_validates_arguments() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    with pytest.raises(ValueError, match="must be non-empty"):
        client.exchange_code(code="", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0)
    with pytest.raises(ValueError, match="must be non-empty"):
        client.refresh(refresh_token="", now=T0)


def test_construction_requires_credentials() -> None:
    with pytest.raises(ValueError, match="client_id must be"):
        GoogleHealthOAuthClient(client_id="", client_secret=CLIENT_SECRET)
    with pytest.raises(ValueError, match="client_secret must be"):
        GoogleHealthOAuthClient(client_id=CLIENT_ID, client_secret="  ")


# ---------------------------------------------------------------------------
# Secret leak resistance
# ---------------------------------------------------------------------------


def test_client_repr_redacts_credentials() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    rendered = repr(client)
    assert CLIENT_SECRET not in rendered
    assert CLIENT_ID not in rendered
    assert PROVIDER in rendered


def test_tokens_repr_redacts_token_values() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    result = client.exchange_code(
        code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
    )
    assert result.tokens is not None
    rendered = repr(result.tokens)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert "<redacted>" in rendered


def test_error_logs_never_contain_secrets_or_bodies() -> None:
    """A token endpoint body holds credentials, so it must never be logged."""
    leaky_body = {
        "error": "invalid_grant",
        "error_description": f"token {ACCESS_TOKEN} rejected",
        "refresh_token": REFRESH_TOKEN,
    }
    client, _ = _client(_json_response(400, leaky_body))

    with _captured_logs() as captured:
        client.exchange_code(
            code="auth-code", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0
        )

    rendered = captured.rendered()
    assert "invalid_grant" in rendered  # positive control
    for secret in (CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN, CODE_VERIFIER):
        assert secret not in rendered


def test_transport_error_logs_no_request_body() -> None:
    def boom(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"failed sending client_secret={CLIENT_SECRET}")

    client, _ = _client(boom)

    with _captured_logs() as captured:
        client.exchange_code(code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0)

    rendered = captured.rendered()
    assert "transport error" in rendered  # positive control
    assert CLIENT_SECRET not in rendered
    assert CODE_VERIFIER not in rendered
