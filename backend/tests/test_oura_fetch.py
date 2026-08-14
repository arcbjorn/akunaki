"""Tests for the Oura V2 fetch client (mock transport, no network).

Covers the two window shapes the V2 collections use — local dates for the daily
collections, instants for the continuously-sampled heart-rate series — and how
vendor rejections map onto the retry vocabulary.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx2

from akunaki.adapters.connectors.oura_fetch import OuraFetchClient
from akunaki.domain.fetch import FetchFailure

T0 = datetime(2026, 8, 14, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 15, tzinfo=UTC)


def _client(responder: Callable[[httpx2.Request], httpx2.Response]) -> OuraFetchClient:
    return OuraFetchClient(transport=httpx2.Client(transport=httpx2.MockTransport(responder)))


def _fetch(client: OuraFetchClient, *, stream: str = "sleep") -> object:
    return client.fetch_page(
        access_token="AT",
        stream=stream,
        window_start=WINDOW_START,
        window_end=T0,
        page_token=None,
        now=T0,
    )


def test_client_error_is_not_retried() -> None:
    """A 4xx is our request being wrong, and will be refused every time.

    Retrying buries a wiring bug — a too-wide window, a misspelled parameter —
    under a generic transient label until the attempt budget is gone.
    """

    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"detail": "Timerange ... 30 days"})

    result = _fetch(_client(responder))

    assert result.failure is FetchFailure.MALFORMED_REQUEST  # type: ignore[attr-defined]
    assert result.failure.retryable is False  # type: ignore[attr-defined]


def test_server_error_is_still_retried() -> None:
    """A 5xx is the vendor being unwell; the same request may well succeed."""

    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, text="down")

    result = _fetch(_client(responder))

    assert result.failure is FetchFailure.PROVIDER_ERROR  # type: ignore[attr-defined]
    assert result.failure.retryable is True  # type: ignore[attr-defined]


def test_auth_rejection_stays_unauthorized() -> None:
    """401/403 must keep flipping the connection, not read as a bad request."""

    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="nope")

    result = _fetch(_client(responder))

    assert result.failure is FetchFailure.UNAUTHORIZED  # type: ignore[attr-defined]


def test_rate_limit_stays_retryable() -> None:
    def responder(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, text="slow", headers={"retry-after": "30"})

    result = _fetch(_client(responder))

    assert result.failure is FetchFailure.RATE_LIMIT  # type: ignore[attr-defined]
    assert result.retry_after_seconds == 30  # type: ignore[attr-defined]


def test_daily_collections_window_on_local_dates() -> None:
    captured: dict[str, str] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured.update(dict(request.url.params))
        return httpx2.Response(200, json={"data": []})

    _fetch(_client(responder), stream="daily_activity")

    assert captured["start_date"] == "2026-07-15"
    assert captured["end_date"] == "2026-08-14"


def test_heartrate_windows_on_instants_not_dates() -> None:
    """The continuously-sampled series rejects date parameters."""
    captured: dict[str, str] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured.update(dict(request.url.params))
        return httpx2.Response(200, json={"data": []})

    _fetch(_client(responder), stream="heartrate")

    assert "start_datetime" in captured
    assert "start_date" not in captured


def test_heartrate_window_is_clamped_to_the_vendor_maximum() -> None:
    """Oura refuses a heart-rate range wider than 30 days.

    The default backfill asks for 30 days *plus* a 36-hour overlap, which is
    just over the line — so without clamping this stream never ingests anything.
    """
    captured: dict[str, str] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured.update(dict(request.url.params))
        return httpx2.Response(200, json={"data": []})

    _client(responder).fetch_page(
        access_token="AT",
        stream="heartrate",
        window_start=T0 - timedelta(days=30, hours=36),
        window_end=T0,
        page_token=None,
        now=T0,
    )

    start = datetime.fromisoformat(captured["start_datetime"].replace("Z", "+00:00"))
    assert (T0 - start) <= timedelta(days=30)


def test_daily_collections_are_not_clamped() -> None:
    """Only the capped stream is trimmed; the rest keep the caller's window."""
    captured: dict[str, str] = {}

    def responder(request: httpx2.Request) -> httpx2.Response:
        captured.update(dict(request.url.params))
        return httpx2.Response(200, json={"data": []})

    _client(responder).fetch_page(
        access_token="AT",
        stream="daily_activity",
        window_start=T0 - timedelta(days=90),
        window_end=T0,
        page_token=None,
        now=T0,
    )

    assert captured["start_date"] == (T0 - timedelta(days=90)).date().isoformat()
