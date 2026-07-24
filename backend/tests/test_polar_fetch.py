"""Tests for the Polar AccessLink fetch client (mock transport, no network).

The client drives the AccessLink exercise **transaction** lifecycle: open a
transaction (POST), list its exercise URLs (GET), fetch each summary and its
heart-rate zones (GET), then commit (PUT). The responder below routes on method
and path to model that flow.
"""

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

_TX = "https://www.polaraccesslink.com/v3/users/self/exercise-transactions/42"
_EX = "https://www.polaraccesslink.com/v3/users/self/exercise-transactions/42/exercises/ex-1"

_SUMMARY = {
    "id": "ex-1",
    "start-time": "2026-07-22T06:00:00.000",
    "duration": "PT1H",
    "sport": "RUNNING",
}
_ZONES = {"zone": [{"index": i, "in-zone": "PT10M"} for i in range(1, 6)]}


def _transactional_responder(
    calls: list[tuple[str, str]],
) -> Callable[[httpx2.Request], httpx2.Response]:
    """Model the create → list → summary → zones → commit flow."""

    def responder(request: httpx2.Request) -> httpx2.Response:
        method = request.method
        url = str(request.url)
        calls.append((method, url))
        json_headers = {"content-type": "application/json"}
        if method == "POST" and url.endswith("/exercise-transactions"):
            return httpx2.Response(201, json={"resource-uri": _TX}, headers=json_headers)
        if method == "GET" and url == _TX:
            return httpx2.Response(200, json={"exercises": [_EX]}, headers=json_headers)
        if method == "GET" and url == _EX:
            return httpx2.Response(200, json=_SUMMARY, headers=json_headers)
        if method == "GET" and url == f"{_EX}/heart-rate-zones":
            return httpx2.Response(200, json=_ZONES, headers=json_headers)
        if method == "PUT" and url == _TX:
            return httpx2.Response(200, headers=json_headers)
        return httpx2.Response(500, text="unexpected")

    return responder


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


def test_drains_transaction_and_assembles_exercises() -> None:
    calls: list[tuple[str, str]] = []
    result = _fetch(_client(_transactional_responder(calls)))

    assert result.failure is None  # type: ignore[attr-defined]
    envelope = result.envelope  # type: ignore[attr-defined]
    assert envelope is not None
    assert envelope.provider == "polar"
    assert envelope.stream == "workout"
    assert envelope.next_page_token is None

    # The assembled page carries the exercise with its zones inlined.
    body = json.loads(envelope.payload_text)
    assert [ex["id"] for ex in body["exercises"]] == ["ex-1"]
    assert body["exercises"][0]["heart_rate_zones"] == _ZONES["zone"]

    # The full lifecycle ran, ending in a commit.
    methods = [m for m, _ in calls]
    assert methods == ["POST", "GET", "GET", "GET", "PUT"]
    assert calls[0][0] == "POST" and calls[0][1].endswith("/exercise-transactions")
    assert calls[-1] == ("PUT", _TX)


def test_auth_header_carries_the_token_but_meta_does_not() -> None:
    captured: dict[str, str | None] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured.setdefault("auth", request.headers.get("authorization"))
        return _transactional_responder([])(request)

    result = _fetch(_client(responder))
    envelope = result.envelope  # type: ignore[attr-defined]
    assert captured["auth"] == "Bearer AT"
    assert "AT" not in json.dumps(envelope.request_meta)


def test_no_new_data_204_is_an_empty_page() -> None:
    def responder(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(204)
        return httpx2.Response(500, text="should not be called")

    result = _fetch(_client(responder))
    envelope = result.envelope  # type: ignore[attr-defined]
    assert envelope is not None
    assert json.loads(envelope.payload_text) == {"exercises": []}


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


def test_401_on_open_is_unauthorized() -> None:
    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="nope")

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.UNAUTHORIZED  # type: ignore[attr-defined]


def test_429_on_open_is_rate_limit_with_retry_after() -> None:
    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, text="slow down", headers={"retry-after": "30"})

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.RATE_LIMIT  # type: ignore[attr-defined]
    assert result.retry_after_seconds == 30  # type: ignore[attr-defined]


def test_500_on_summary_is_provider_error() -> None:
    def responder(request: httpx2.Request) -> httpx2.Response:
        json_headers = {"content-type": "application/json"}
        if request.method == "POST":
            return httpx2.Response(201, json={"resource-uri": _TX}, headers=json_headers)
        if request.method == "GET" and str(request.url) == _TX:
            return httpx2.Response(200, json={"exercises": [_EX]}, headers=json_headers)
        return httpx2.Response(503, text="down")

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.PROVIDER_ERROR  # type: ignore[attr-defined]


def test_missing_resource_uri_is_malformed() -> None:
    def responder(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(
                201, json={"nope": 1}, headers={"content-type": "application/json"}
            )
        return httpx2.Response(500, text="unexpected")

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.MALFORMED_RESPONSE  # type: ignore[attr-defined]


def test_non_json_transaction_body_is_malformed() -> None:
    def responder(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, text="not json", headers={"content-type": "text/plain"})

    result = _fetch(_client(responder))
    assert result.failure is FetchFailure.MALFORMED_RESPONSE  # type: ignore[attr-defined]
