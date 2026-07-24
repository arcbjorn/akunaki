"""Polar AccessLink data fetch client.

Returns the **exact** vendor objects so the transport layer can persist a
faithful record; nothing here reinterprets or reshapes vendor data. Mirrors the
Oura client's secrets discipline: the access token rides in the Authorization
header and is never logged, and response bodies never reach log records or
exceptions.

Polar AccessLink v3 exposes exercises through a **transaction** lifecycle, not
a plain collection GET:

1. ``POST .../exercise-transactions`` opens a transaction over the not-yet-seen
   exercises. A ``201`` returns a ``resource-uri`` (the transaction URL); a
   ``204`` means there is nothing new (an empty, valid page).
2. ``GET {resource-uri}`` lists the exercise resource URLs in the transaction.
3. Each exercise's summary (``GET {url}``) and its HR-zone durations
   (``GET {url}/heart-rate-zones``) are fetched and the zones inlined onto the
   summary under ``heart_rate_zones`` — the shape the workout normalizer reads.
4. ``PUT {resource-uri}`` **commits**, so the same exercises are not returned
   again on the next sync.

The collected exercise objects are assembled into one ``{"exercises": [...]}``
body — the faithful record of exactly what the vendor returned, retained and
hashed like any other page. Only the ``workout`` stream is supported in v0.1.0;
the transaction is a single page, so ``next_page_token`` is always None.

The authenticated user is addressed as ``self`` in every path, so no vendor
user id needs threading through the sync loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

import httpx2

from akunaki.domain.fetch import FetchFailure, FetchResult, RawEnvelope
from akunaki.domain.jobs import require_aware, to_utc_rfc3339

logger = logging.getLogger("akunaki.connectors.polar_fetch")

PROVIDER = "polar"
API_BASE = "https://www.polaraccesslink.com/v3"
DEFAULT_TIMEOUT_SECONDS = 30.0

# The authenticated user is addressed as `self` in AccessLink v3 paths.
_USER = "self"

# Streams this connector can fetch, mapped to their AccessLink transaction kind.
STREAM_TRANSACTIONS = {
    "workout": "exercise-transactions",
}
# Bound on how many exercises one transaction will drain, so a runaway backlog
# cannot make a single job unbounded.
_MAX_EXERCISES = 100


class PolarFetchClient:
    """Fetch Polar AccessLink exercises through the transaction lifecycle."""

    def __init__(
        self,
        *,
        transport: httpx2.Client | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        api_base: str = API_BASE,
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds
        self._api_base = api_base

    @property
    def provider(self) -> str:
        """Provider identifier."""
        return PROVIDER

    def __repr__(self) -> str:
        """No credentials are held on this client, but keep the repr minimal."""
        return f"PolarFetchClient(provider={PROVIDER!r})"

    def fetch_page(
        self,
        *,
        access_token: str,
        stream: str,
        window_start: datetime,
        window_end: datetime,
        page_token: str | None,
        now: datetime,
    ) -> FetchResult:
        """Drain one exercise transaction into a single faithful page.

        The transaction covers whatever the vendor has not yet delivered, so the
        window bounds are validated (for a uniform connector contract) but not
        sent; there is never a next-page token.
        """
        if not access_token:
            msg = "access_token must be non-empty"
            raise ValueError(msg)
        transaction_kind = STREAM_TRANSACTIONS.get(stream)
        if transaction_kind is None:
            msg = f"unsupported Polar stream {stream!r}"
            raise ValueError(msg)

        start = require_aware(window_start, field_name="window_start")
        end = require_aware(window_end, field_name="window_end")
        if end < start:
            msg = "window_end must not precede window_start"
            raise ValueError(msg)

        base = f"{self._api_base}/users/{_USER}/{transaction_kind}"
        try:
            return self._drain_transaction(
                base=base, access_token=access_token, stream=stream, now=now
            )
        except httpx2.HTTPError:
            # The exception text can echo the request, which carries the token.
            logger.warning("polar fetch transport error", extra={"stream": stream})
            return FetchResult(failure=FetchFailure.TRANSPORT_ERROR)

    def _drain_transaction(
        self, *, base: str, access_token: str, stream: str, now: datetime
    ) -> FetchResult:
        """Open, read, commit one transaction; assemble its exercises."""
        # 1. Open the transaction over not-yet-seen exercises.
        opened = self._request("POST", base, access_token)
        if opened.status_code == 204:
            return self._empty_page(stream=stream, now=now)
        if opened.status_code >= 400:
            return self._classify_error(opened, stream=stream)

        transaction_url = _transaction_url(opened)
        if transaction_url is None:
            logger.warning(
                "polar transaction response missing resource-uri",
                extra={"stream": stream},
            )
            return FetchResult(failure=FetchFailure.MALFORMED_RESPONSE)

        # 2. List the exercise resource URLs in the transaction.
        listed = self._request("GET", transaction_url, access_token)
        if listed.status_code >= 400:
            return self._classify_error(listed, stream=stream)
        try:
            listed_body = listed.json()
        except ValueError:
            return FetchResult(failure=FetchFailure.MALFORMED_RESPONSE)
        exercise_urls = _exercise_urls(listed_body)

        # 3. Fetch each exercise summary and inline its HR-zone durations.
        exercises: list[dict[str, object]] = []
        for url in exercise_urls[:_MAX_EXERCISES]:
            summary = self._request("GET", url, access_token)
            if summary.status_code >= 400:
                return self._classify_error(summary, stream=stream)
            try:
                exercise = summary.json()
            except ValueError:
                return FetchResult(failure=FetchFailure.MALFORMED_RESPONSE)
            if not isinstance(exercise, dict):
                continue

            zones = self._request("GET", f"{url}/heart-rate-zones", access_token)
            if zones.status_code >= 400:
                return self._classify_error(zones, stream=stream)
            try:
                zone_body = zones.json()
            except ValueError:
                return FetchResult(failure=FetchFailure.MALFORMED_RESPONSE)
            zone_list = _zone_list(zone_body)
            if zone_list is not None:
                exercise["heart_rate_zones"] = zone_list
            exercises.append(exercise)

        # 4. Commit so these exercises are not delivered again.
        committed = self._request("PUT", transaction_url, access_token)
        if committed.status_code >= 400:
            return self._classify_error(committed, stream=stream)

        body = json.dumps({"exercises": exercises}, sort_keys=True)
        return self._page(body=body, stream=stream, now=now)

    def _empty_page(self, *, stream: str, now: datetime) -> FetchResult:
        """A valid page with no exercises (vendor 204: nothing new)."""
        return self._page(body=json.dumps({"exercises": []}), stream=stream, now=now)

    def _page(self, *, body: str, stream: str, now: datetime) -> FetchResult:
        return FetchResult(
            envelope=RawEnvelope(
                provider=PROVIDER,
                stream=stream,
                payload_text=body,
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                http_status=200,
                content_type="application/json",
                fetched_at=to_utc_rfc3339(require_aware(now, field_name="now")),
                # Redacted: a path template only, never the token.
                request_meta={"url_template": f"v3/users/{_USER}/exercise-transactions"},
                page_token=None,
                next_page_token=None,
            )
        )

    def _request(self, method: str, url: str, access_token: str) -> httpx2.Response:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if self._transport is not None:
            return self._transport.request(
                method, url, headers=headers, timeout=self._timeout
            )
        with httpx2.Client(timeout=self._timeout) as client:
            return client.request(method, url, headers=headers)

    def _classify_error(self, response: httpx2.Response, *, stream: str) -> FetchResult:
        """Map a non-2xx response to a typed failure. The body is never logged."""
        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("retry-after"))

        if status in (401, 403):
            failure = FetchFailure.UNAUTHORIZED
        elif status == 429:
            failure = FetchFailure.RATE_LIMIT
        else:
            failure = FetchFailure.PROVIDER_ERROR

        logger.warning(
            "polar fetch rejected",
            extra={
                "stream": stream,
                "status": status,
                "failure": str(failure),
                "retry_after_seconds": retry_after,
            },
        )
        return FetchResult(failure=failure, retry_after_seconds=retry_after)


def _transaction_url(response: httpx2.Response) -> str | None:
    """Extract the transaction ``resource-uri`` from an opened transaction."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    raw = body.get("resource-uri")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _exercise_urls(body: object) -> list[str]:
    """Extract the exercise resource URLs listed in a transaction."""
    if not isinstance(body, dict):
        return []
    raw = body.get("exercises")
    if not isinstance(raw, list):
        return []
    return [url for url in raw if isinstance(url, str) and url]


def _zone_list(body: object) -> list[object] | None:
    """Extract the ``zone`` array from a heart-rate-zones response."""
    if not isinstance(body, dict):
        return None
    raw = body.get("zone")
    if isinstance(raw, list):
        return raw
    return None


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` delta-seconds header, ignoring HTTP-date form."""
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return max(seconds, 0)
