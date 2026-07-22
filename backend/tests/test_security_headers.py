"""Security headers and CORS on the API surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from akunaki.api.app import create_app
from akunaki.config import Settings


def _client(**overrides: object) -> TestClient:
    base: dict[str, object] = {"database_url": "sqlite+libsql:///:memory:"}
    base.update(overrides)
    return TestClient(create_app(Settings(**base)))  # type: ignore[arg-type]


def test_security_headers_on_every_response() -> None:
    # /healthz needs no session, so it exercises the middleware on a 200.
    response = _client().get("/healthz")
    headers = response.headers
    assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_security_headers_on_error_responses() -> None:
    # A 401 (no session) still carries the security headers.
    response = _client().get("/v1/session")
    assert response.status_code == 401
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


def test_no_cors_headers_without_an_allow_list() -> None:
    # Default (empty allow-list): a cross-origin request gets no CORS grant.
    response = _client().get("/healthz", headers={"origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_configured_origin() -> None:
    client = _client(cors_allowed_origins=("https://app.example.com",))
    response = client.get("/healthz", headers={"origin": "https://app.example.com"})
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_rejects_unlisted_origin() -> None:
    client = _client(cors_allowed_origins=("https://app.example.com",))
    response = client.get("/healthz", headers={"origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers
