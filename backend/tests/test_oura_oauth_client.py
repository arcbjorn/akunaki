"""Oura OAuth client: authorize URL, PKCE exchange, and failure mapping.

All network traffic is served by an in-process mock transport; these tests
never reach the real Oura API. The security assertions are as important as the
happy path: no client secret, code verifier, or token value may appear in a log
record, exception, or repr.
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

from akunaki.adapters.connectors.oura import PROVIDER, OuraOAuthClient
from akunaki.domain.tokens import TokenExchangeFailure

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
CLIENT_ID = "oura-client-id"
CLIENT_SECRET = "oura-client-SECRET-value"
REDIRECT = "https://app.example.com/oauth/oura/callback"
CODE_VERIFIER = "v" * 64
ACCESS_TOKEN = "oura-access-TOKEN-value"
REFRESH_TOKEN = "oura-refresh-TOKEN-value"

TOKEN_BODY = {
    "access_token": ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
    "expires_in": 86400,
    "scope": "daily personal",
    "token_type": "Bearer",
}


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[OuraOAuthClient, list[httpx2.Request]]:
    """Build a client whose transport records every request it sends."""
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    transport = httpx2.Client(transport=httpx2.MockTransport(recording))
    return (
        OuraOAuthClient(
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


OURA_LOGGER = "akunaki.connectors.oura"


class _Capture(logging.Handler):
    """Collect records directly off the target logger.

    Deliberately not ``caplog``: that attaches at the root, and anything which
    reconfigures root handlers mid-suite (``logging.config.fileConfig`` during
    an Alembic migration does exactly this) silently yields an empty capture —
    which would make every leak assertion below pass vacuously.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def rendered(self) -> str:
        """Render messages **and** structured ``extra`` fields, both leak surfaces."""
        return "\n".join(
            [record.getMessage() for record in self.records]
            + [json.dumps(record.__dict__, default=str) for record in self.records]
        )


@contextmanager
def _captured_logs() -> Iterator[_Capture]:
    """Attach a private handler to the Oura logger for the duration."""
    logger = logging.getLogger(OURA_LOGGER)
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


def test_authorize_url_carries_pkce_s256_and_state() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    url = client.authorize_url(
        state="state-abc",
        code_challenge="challenge-xyz",
        redirect_uri=REDIRECT,
        scopes=("daily", "personal"),
    )

    parsed = urlparse(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert parsed.netloc == "cloud.ouraring.com"
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT
    assert params["state"] == "state-abc"
    assert params["code_challenge"] == "challenge-xyz"
    # `plain` would offer no protection against a leaked authorization code.
    assert params["code_challenge_method"] == "S256"
    assert params["scope"] == "daily personal"


def test_authorize_url_never_contains_the_client_secret() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    url = client.authorize_url(
        state="s",
        code_challenge="c",
        redirect_uri=REDIRECT,
        scopes=("daily",),
    )
    assert CLIENT_SECRET not in url


def test_authorize_url_validates_arguments() -> None:
    client, _ = _client(_json_response(200, TOKEN_BODY))
    for field in ("state", "code_challenge", "redirect_uri"):
        kwargs = {
            "state": "s",
            "code_challenge": "c",
            "redirect_uri": REDIRECT,
            "scopes": ("daily",),
        }
        kwargs[field] = ""
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            client.authorize_url(**kwargs)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="at least one scope"):
        client.authorize_url(state="s", code_challenge="c", redirect_uri=REDIRECT, scopes=())


# ---------------------------------------------------------------------------
# Code exchange
# ---------------------------------------------------------------------------


def test_exchange_code_sends_pkce_verifier_and_returns_tokens() -> None:
    client, seen = _client(_json_response(200, TOKEN_BODY))

    result = client.exchange_code(
        code="auth-code-1",
        code_verifier=CODE_VERIFIER,
        redirect_uri=REDIRECT,
        now=T0,
    )

    assert result.ok
    assert result.tokens is not None
    assert result.tokens.access_token == ACCESS_TOKEN
    assert result.tokens.refresh_token == REFRESH_TOKEN
    assert result.tokens.scopes == ("daily", "personal")
    # expires_in is converted to an absolute instant so it survives a restart.
    assert result.tokens.expires_at == "2026-07-20T12:00:00Z"

    form = _form_of(seen[0])
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code-1"
    assert form["code_verifier"] == CODE_VERIFIER
    assert form["redirect_uri"] == REDIRECT
    assert form["client_id"] == CLIENT_ID


def test_refresh_sends_refresh_grant() -> None:
    client, seen = _client(_json_response(200, TOKEN_BODY))

    result = client.refresh(refresh_token=REFRESH_TOKEN, now=T0)

    assert result.ok
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
    # These must drive needs_reauth, never a retry loop.
    assert expected.retryable is False


def test_server_error_is_retryable() -> None:
    client, _ = _client(_json_response(503, {"error": "temporarily_unavailable"}))

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
        OuraOAuthClient(client_id="", client_secret=CLIENT_SECRET)
    with pytest.raises(ValueError, match="client_secret must be"):
        OuraOAuthClient(client_id=CLIENT_ID, client_secret="  ")


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

    # A traceback that renders tokens would leak them into logs wholesale.
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
    # Positive control first: an empty capture would make the leak assertions
    # below pass vacuously, so prove records were actually captured.
    assert "invalid_grant" in rendered
    for secret in (CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN, CODE_VERIFIER):
        assert secret not in rendered


def test_transport_error_logs_no_request_body() -> None:
    def boom(_request: httpx2.Request) -> httpx2.Response:
        # A real transport error can carry the request, which holds the secret.
        raise httpx2.ConnectError(f"failed sending client_secret={CLIENT_SECRET}")

    client, _ = _client(boom)

    with _captured_logs() as captured:
        client.exchange_code(code="c", code_verifier=CODE_VERIFIER, redirect_uri=REDIRECT, now=T0)

    rendered = captured.rendered()
    # Positive control: prove the transport-error path actually logged.
    assert "transport error" in rendered
    assert CLIENT_SECRET not in rendered
    assert CODE_VERIFIER not in rendered
