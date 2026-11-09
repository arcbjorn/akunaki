"""Tests for the Google Health v4 fetch client (mock transport, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx2
import pytest

from akunaki.adapters.connectors.google_health_fetch import GoogleHealthFetchClient
from akunaki.domain.fetch import FetchFailure, FetchResult

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 6, 24, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 22, tzinfo=UTC)

_SLEEP_PAGE = json.dumps(
    {
        "dataPoints": [
            {
                "name": "dp-1",
                "sleep": {
                    "interval": {
                        "startTime": "2026-07-22T00:10:00Z",
                        "endTime": "2026-07-22T07:50:00Z",
                    },
                    "stages": [
                        {
                            "startTime": "2026-07-22T00:10:00Z",
                            "endTime": "2026-07-22T07:50:00Z",
                            "type": "DEEP",
                        }
                    ],
                },
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


def test_list_gets_window_filter_and_returns_exact_body() -> None:
    captured: dict[str, object] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["params"] = dict(request.url.params)
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

    # List is a GET whose path names the data type and whose filter windows it.
    assert captured["method"] == "GET"
    # The v4 data type id is the bare name, never a legacy `com.google.*` one.
    assert str(captured["url"]).split("?")[0].endswith("/v4/users/me/dataTypes/sleep/dataPoints")
    assert "com.google" not in str(captured["url"])
    assert captured["auth"] == "Bearer AT"
    params = captured["params"]
    assert isinstance(params, dict)
    filter_expr = params["filter"]
    # Sleep windows on the session's END time. v4 supports only end-time
    # filtering for sleep and rejects `sleep.interval.start_time` with
    # INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER, so asserting the start-time
    # spelling here is what let a 400-on-every-call ship green once already.
    assert 'sleep.interval.end_time >= "2026-06-24T00:00:00Z"' in filter_expr
    assert 'sleep.interval.end_time < "2026-07-22T00:00:00Z"' in filter_expr
    assert "start_time" not in filter_expr
    assert params["pageSize"] == "25"
    assert "pageToken" not in params
    # The redacted request meta carries no token.
    assert "AT" not in json.dumps(envelope.request_meta)


def test_page_token_advances_the_request() -> None:
    captured: dict[str, object] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured["params"] = dict(request.url.params)
        return httpx2.Response(200, text=json.dumps({"dataPoints": []}))

    result = _fetch(_client(responder), page_token="page-2")
    envelope = result.envelope  # type: ignore[attr-defined]
    assert envelope is not None
    # A page with no nextPageToken ends the loop.
    assert envelope.next_page_token is None
    assert envelope.page_token == "page-2"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["pageToken"] == "page-2"


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


def test_400_is_malformed_request_and_not_retried() -> None:
    def refused(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": {"message": "Invalid argument in request"}})

    result = _fetch(_client(refused))
    assert isinstance(result, FetchResult)
    assert result.failure is FetchFailure.MALFORMED_REQUEST
    assert not result.failure.retryable


def test_steps_rollup_window_is_clamped_to_90_days() -> None:
    """A ten-year backfill window must reach Google as at most 90 days."""
    bodies: list[dict[str, object]] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"rollupDataPoints": []})

    result = _client(capture).fetch_page(
        access_token="AT",
        stream="steps",
        window_start=datetime(2016, 7, 22, tzinfo=UTC),
        window_end=WINDOW_END,
        page_token=None,
        now=T0,
    )
    assert result.ok
    assert len(bodies) == 1
    start = bodies[0]["range"]["start"]["date"]  # type: ignore[index]
    assert (start["year"], start["month"], start["day"]) == (2026, 4, 23)


def test_sleep_list_window_is_clamped_to_90_days() -> None:
    seen: list[str] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params["filter"])
        return httpx2.Response(200, json={"dataPoints": []})

    result = _client(capture).fetch_page(
        access_token="AT",
        stream="sleep",
        window_start=datetime(2016, 7, 22, tzinfo=UTC),
        window_end=WINDOW_END,
        page_token=None,
        now=T0,
    )
    assert result.ok
    assert seen[0].startswith('sleep.interval.end_time >= "2026-04-23T00:00:00Z"')


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


def test_sleep_never_filters_on_start_time() -> None:
    """v4 supports only END-time filtering for sleep sessions.

    Filtering on `sleep.interval.start_time` is rejected outright with
    `INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER` ("Member ... is not supported
    for filtering"), so every fetch 400s and no sleep is ever ingested. That
    shipped once, green, because the test asserted the same wrong member the
    client sent — this pins the vendor's constraint rather than our spelling.
    """
    captured: dict[str, object] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured["filter"] = dict(request.url.params).get("filter", "")
        return httpx2.Response(200, text=_SLEEP_PAGE)

    _fetch(_client(responder))

    filter_expr = str(captured["filter"])
    assert "sleep.interval.end_time" in filter_expr
    # The rejected member must not appear in any position.
    assert "start_time" not in filter_expr
    assert "startTime" not in filter_expr


def test_a_window_wider_than_the_ceiling_is_walked_in_pieces() -> None:
    """A 30-day active-minutes backfill reaches Google as three requests, newest first.

    The vendor caps `active-minutes` at 14 days per request. Rather than
    silently dropping the older 16 days, the client hands back a synthetic
    page token that resumes the walk where the last piece began, so the
    handler's ordinary page loop fetches the whole window.
    """
    bodies: list[dict[str, object]] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"rollupDataPoints": []})

    client = _client(capture)
    token: str | None = None
    for _ in range(5):
        result = client.fetch_page(
            access_token="AT",
            stream="active_minutes",
            window_start=datetime(2026, 6, 22, tzinfo=UTC),
            window_end=WINDOW_END,
            page_token=token,
            now=T0,
        )
        assert result.ok
        assert result.envelope is not None
        token = result.envelope.next_page_token
        if token is None:
            break

    def day(body: dict[str, object], edge: str) -> int:
        return body["range"][edge]["date"]["day"]  # type: ignore[index,no-any-return]

    assert [(day(b, "start"), day(b, "end")) for b in bodies] == [(8, 22), (24, 8), (22, 24)]
    assert token is None
    # A pageToken from the vendor is never confused with the walk's own token.
    assert all("pageToken" not in b for b in bodies)
