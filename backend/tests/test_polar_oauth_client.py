"""Polar OAuth client: authorize URL, Basic-auth code exchange, failure mapping.

All network traffic is served by an in-process mock transport; these tests
never reach the real Polar API. The security assertions are as important as the
happy path: no client secret or token value may appear in a log record,
exception, or repr. Polar-specific behavior verified here: Basic auth on the
token request (secret in the header, never the form body), no PKCE, no refresh
token, and capture of the ``x_user_id`` as the external user id.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from akunaki.adapters.connectors.polar import PROVIDER, PolarOAuthClient
from akunaki.domain.tokens import TokenExchangeFailure

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
CLIENT_ID = "polar-client-id"
CLIENT_SECRET = "polar-client-SECRET-value"
REDIRECT = "https://app.example.com/oauth/polar/callback"
ACCESS_TOKEN = "polar-access-TOKEN-value"

TOKEN_BODY = {
    "access_token": ACCESS_TOKEN,
    "token_type": "bearer",
    "expires_in": 86400,
    "x_user_id": 987654,
}

POLAR_LOGGER = "akunaki.connectors.polar"


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[PolarOAuthClient, list[httpx2.Request]]:
    """Build a client whose transport records every request it sends."""
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    transport = httpx2.Client(transport=httpx2.MockTransport(recording))
    return (
        PolarOAuthClient(
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
    """Collect records directly off the target logger (not caplog; see Oura)."""

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
    logger = logging.getLogger(POLAR_LOGGER)
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


def test_authorize_url_has_state_and_no_pkce() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    url = client.authorize_url(
        state="state-abc",
        redirect_uri=REDIRECT,
        scopes=("accesslink.read_all",),
    )

    parsed = urlparse(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert parsed.netloc == "flow.polar.com"
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT
    assert params["state"] == "state-abc"
    assert params["scope"] == "accesslink.read_all"
    # Polar does not use PKCE.
    assert "code_challenge" not in params
    assert "code_challenge_method" not in params


def test_authorize_url_defaults_to_read_all_scope() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    url = client.authorize_url(state="s", redirect_uri=REDIRECT)
    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert params["scope"] == "accesslink.read_all"


def test_authorize_url_never_contains_the_client_secret() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    url = client.authorize_url(state="s", redirect_uri=REDIRECT)
    assert CLIENT_SECRET not in url


def test_authorize_url_validates_arguments() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    for field in ("state", "redirect_uri"):
        kwargs = {"state": "s", "redirect_uri": REDIRECT}
        kwargs[field] = ""
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            client.authorize_url(**kwargs)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="at least one scope"):
        client.authorize_url(state="s", redirect_uri=REDIRECT, scopes=())


# ---------------------------------------------------------------------------
# Code exchange
# ---------------------------------------------------------------------------


def test_exchange_uses_basic_auth_and_captures_user_id() -> None:
    client, seen = _client(_json_response(200, TOKEN_BODY))

    result = client.exchange_code(code="auth-code-1", redirect_uri=REDIRECT, now=T0)

    assert result.ok
    assert result.tokens is not None
    assert result.tokens.access_token == ACCESS_TOKEN
    # Polar issues no refresh token; the access token is long-lived.
    assert result.tokens.refresh_token is None
    # expires_in is converted to an absolute instant so it survives a restart.
    assert result.tokens.expires_at == "2026-07-23T12:00:00Z"
    # x_user_id becomes the connection's external user id (stringified).
    assert result.tokens.external_user_id == "987654"

    request = seen[0]
    form = _form_of(request)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code-1"
    assert form["redirect_uri"] == REDIRECT
    # The secret authenticates via Basic auth, NOT a form field.
    assert "client_secret" not in form
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


def test_missing_optional_fields_are_tolerated() -> None:
    client, _ = _client(_json_response(200, {"access_token": ACCESS_TOKEN}))

    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)

    assert result.ok
    assert result.tokens is not None
    assert result.tokens.refresh_token is None
    assert result.tokens.expires_at is None
    assert result.tokens.external_user_id is None
    assert result.tokens.token_type == "Bearer"


def test_boolean_user_id_is_ignored() -> None:
    # A bool is an int subclass; it must not be read as a user id.
    client, _ = _client(_json_response(200, {"access_token": ACCESS_TOKEN, "x_user_id": True}))
    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)
    assert result.ok
    assert result.tokens is not None
    assert result.tokens.external_user_id is None


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

    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)

    assert not result.ok
    assert result.failure is expected
    assert expected.retryable is False


def test_server_error_is_retryable() -> None:
    client, _ = _client(_json_response(503, {"error": "temporarily_unavailable"}))
    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)
    assert result.failure is TokenExchangeFailure.PROVIDER_ERROR
    assert result.failure.retryable is True


def test_transport_error_is_retryable() -> None:
    def boom(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    client, _ = _client(boom)
    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)
    assert result.failure is TokenExchangeFailure.TRANSPORT_ERROR
    assert result.failure.retryable is True


def test_non_json_response_is_malformed() -> None:
    def html(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>gateway</html>")

    client, _ = _client(html)
    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)
    assert result.failure is TokenExchangeFailure.MALFORMED_RESPONSE


def test_response_without_access_token_is_malformed() -> None:
    client, _ = _client(_json_response(200, {"token_type": "bearer"}))
    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)
    assert result.failure is TokenExchangeFailure.MALFORMED_RESPONSE
    assert result.tokens is None


def test_exchange_validates_arguments() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    with pytest.raises(ValueError, match="must be non-empty"):
        client.exchange_code(code="", redirect_uri=REDIRECT, now=T0)
    with pytest.raises(ValueError, match="must be non-empty"):
        client.exchange_code(code="c", redirect_uri="", now=T0)


def test_construction_requires_credentials() -> None:
    with pytest.raises(ValueError, match="client_id must be"):
        PolarOAuthClient(client_id="", client_secret=CLIENT_SECRET)
    with pytest.raises(ValueError, match="client_secret must be"):
        PolarOAuthClient(client_id=CLIENT_ID, client_secret="  ")


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
    result = client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)
    assert result.tokens is not None
    rendered = repr(result.tokens)
    assert ACCESS_TOKEN not in rendered
    assert "<redacted>" in rendered


def test_error_logs_never_contain_secrets_or_bodies() -> None:
    """A token endpoint body holds credentials, so it must never be logged."""
    leaky_body = {
        "error": "invalid_grant",
        "error_description": f"token {ACCESS_TOKEN} rejected",
    }
    client, _ = _client(_json_response(400, leaky_body))

    with _captured_logs() as captured:
        client.exchange_code(code="auth-code", redirect_uri=REDIRECT, now=T0)

    rendered = captured.rendered()
    # Positive control: prove records were captured before asserting no leak.
    assert "invalid_grant" in rendered
    for secret in (CLIENT_SECRET, ACCESS_TOKEN):
        assert secret not in rendered


def test_transport_error_logs_no_request_body() -> None:
    def boom(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"failed sending client_secret={CLIENT_SECRET}")

    client, _ = _client(boom)

    with _captured_logs() as captured:
        client.exchange_code(code="c", redirect_uri=REDIRECT, now=T0)

    rendered = captured.rendered()
    assert "transport error" in rendered
    assert CLIENT_SECRET not in rendered
