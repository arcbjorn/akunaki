"""Tests for the Polar AccessLink fetch client (mock transport, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx2
import pytest

from akunaki.adapters.connectors.polar_fetch import PolarFetchClient
from akunaki.domain.fetch import FetchFailure

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 6, 24, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 22, tzinfo=UTC)

_EXERCISES = json.dumps(
    [
        {
            "id": "ex-1",
            "start_time": "2026-07-22T06:00:00+02:00",
            "duration": "PT1H",
            "heart_rate_zones": [{"index": i, "in_zone": "PT10M"} for i in range(1, 6)],
        }
    ]
)


def _client(responder: Callable[[httpx2.Request], httpx2.Response]) -> PolarFetchClient:
    return PolarFetchClient(transport=httpx2.Client(transport=httpx2.MockTransport(responder)))


def _fetch(client: PolarFetchClient) -> object:
    return client.fetch_page(
        access_token="AT",
        stream="workout",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        page_token=None,
        now=T0,
    )


def test_fetches_exercises_and_returns_exact_body() -> None:
    captured: dict[str, object] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, text=_EXERCISES, headers={"content-type": "application/json"})

    result = _fetch(_client(responder))
    assert result.failure is None  # type: ignore[attr-defined]
    envelope = result.envelope  # type: ignore[attr-defined]
    assert envelope is not None
    assert envelope.provider == "polar"
    assert envelope.stream == "workout"
    # The exact body is retained, byte for byte.
    assert envelope.payload_text == _EXERCISES
    assert envelope.next_page_token is None
    assert str(captured["url"]).endswith("/v3/exercises")
    assert captured["auth"] == "Bearer AT"
    # The redacted request meta carries no token.
    assert "AT" not in json.dumps(envelope.request_meta)


def test_empty_access_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="access_token must be non-empty"):
        PolarFetchClient().fetch_page(
            access_token="",
            stream="workout",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            page_token=None,
            now=T0,
        )


def test_unsupported_stream_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported Polar stream"):
        PolarFetchClient().fetch_page(
            access_token="AT",
            stream="sleep",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            page_token=None,
            now=T0,
        )


def test_reversed_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="window_end must not precede"):
        PolarFetchClient().fetch_page(
            access_token="AT",
            stream="workout",
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
