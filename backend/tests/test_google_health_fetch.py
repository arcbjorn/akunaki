"""Tests for the Google Health v4 fetch client (mock transport, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx2
import pytest

from akunaki.adapters.connectors.google_health_fetch import GoogleHealthFetchClient
from akunaki.domain.fetch import FetchFailure

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 6, 24, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 22, tzinfo=UTC)

_SLEEP_PAGE = json.dumps(
    {
        "dataPoints": [
            {
                "name": "dp-1",
                "startTime": "2026-07-22T00:10:00Z",
                "endTime": "2026-07-22T07:50:00Z",
                "value": {"sleepType": "SLEEP_STAGE_DEEP"},
            }
        ],
        "nextPageToken": "page-2",
    }
)


def _client(responder: Callable[[httpx2.Request], httpx2.Response]) -> GoogleHealthFetchClient:
    return GoogleHealthFetchClient(
        transport=httpx2.Client(transport=httpx2.MockTransport(responder))
    )


def _fetch(client: GoogleHealthFetchClient, *, page_token: str | None = None) -> object:
    return client.fetch_page(
        access_token="AT",
        stream="sleep",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        page_token=page_token,
        now=T0,
    )


def test_reconcile_posts_window_and_returns_exact_body() -> None:
    captured: dict[str, object] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx2.Response(200, text=_SLEEP_PAGE, headers={"content-type": "application/json"})

    result = _fetch(_client(responder))
    assert result.failure is None  # type: ignore[attr-defined]
    envelope = result.envelope  # type: ignore[attr-defined]
    assert envelope is not None
    assert envelope.provider == "google_health"
    assert envelope.stream == "sleep"
    # The exact body is retained, byte for byte.
    assert envelope.payload_text == _SLEEP_PAGE
    # nextPageToken is surfaced so the backfill loop can advance.
    assert envelope.next_page_token == "page-2"

    # Reconcile is a POST carrying the data type and RFC3339 window.
    assert captured["method"] == "POST"
    assert str(captured["url"]).endswith(
        "/v4/users/me/dataTypes/com.google.sleep.segment/dataPoints:reconcile"
    )
    assert captured["auth"] == "Bearer AT"
    sent = json.loads(str(captured["body"]))
    assert sent["dataTypeName"] == "com.google.sleep.segment"
    assert sent["startTime"] == "2026-06-24T00:00:00Z"
    assert sent["endTime"] == "2026-07-22T00:00:00Z"
    assert "pageToken" not in sent
    # The redacted request meta carries no token.
    assert "AT" not in json.dumps(envelope.request_meta)


def test_page_token_advances_the_request() -> None:
    captured: dict[str, object] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = request.content.decode()
        return httpx2.Response(200, text=json.dumps({"dataPoints": []}))

    result = _fetch(_client(responder), page_token="page-2")
    envelope = result.envelope  # type: ignore[attr-defined]
    assert envelope is not None
    # A page with no nextPageToken ends the loop.
    assert envelope.next_page_token is None
    assert envelope.page_token == "page-2"
    sent = json.loads(str(captured["body"]))
    assert sent["pageToken"] == "page-2"


def test_empty_access_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="access_token must be non-empty"):
        GoogleHealthFetchClient().fetch_page(
            access_token="",
            stream="sleep",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            page_token=None,
            now=T0,
        )


def test_unsupported_stream_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported Google Health stream"):
        GoogleHealthFetchClient().fetch_page(
            access_token="AT",
            stream="workout",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            page_token=None,
            now=T0,
        )


def test_reversed_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="window_end must not precede"):
        GoogleHealthFetchClient().fetch_page(
            access_token="AT",
            stream="sleep",
            window_start=WINDOW_END,
            window_end=WINDOW_START,
            page_token=None,
            now=T0,
        )


def test_401_is_unauthorized() -> None:
    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="nope")

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.UNAUTHORIZED  # type: ignore[attr-defined]


def test_429_is_rate_limit_with_retry_after() -> None:
    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, text="slow down", headers={"retry-after": "30"})

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.RATE_LIMIT  # type: ignore[attr-defined]
    assert result.retry_after_seconds == 30  # type: ignore[attr-defined]


def test_500_is_provider_error() -> None:
    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, text="down")

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.PROVIDER_ERROR  # type: ignore[attr-defined]


def test_non_json_body_is_malformed() -> None:
    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="not json", headers={"content-type": "text/plain"})

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.MALFORMED_RESPONSE  # type: ignore[attr-defined]


def test_repr_names_the_provider() -> None:
    assert "google_health" in repr(GoogleHealthFetchClient())
