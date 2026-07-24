"""Google Health API v4 data fetch client.

Returns the **exact** response body so the transport layer can persist a
faithful record; nothing here reinterprets or reshapes vendor data. Mirrors the
Oura and Polar clients' secrets discipline: the access token rides in the
Authorization header and is never logged, and response bodies never reach log
records or exceptions.

Google Health v4 reads history through ``users.dataTypes.dataPoints.list``
(a **GET**), like the Oura/Polar collection GETs. The window is expressed as an
AIP-160 ``filter`` on the data type's interval, and the response paginates via
``nextPageToken``; the request carries the prior ``pageToken`` to advance.

Only the ``sleep`` stream is supported in v0.1.0 — the Fitbit-origin cloud sleep
path. Google Health is the daytime / Fitbit-origin source the design pairs
against Polar workouts for overlap exclusion.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import httpx2

from akunaki.domain.fetch import FetchFailure, FetchResult, RawEnvelope
from akunaki.domain.jobs import require_aware, to_utc_rfc3339

logger = logging.getLogger("akunaki.connectors.google_health_fetch")

PROVIDER = "google_health"
API_BASE = "https://health.googleapis.com/v4"
DEFAULT_TIMEOUT_SECONDS = 30.0

# Google caps the sleep/exercise data-type page at 25 points per request.
SLEEP_PAGE_SIZE = 25

# Streams this connector can fetch, mapped to their Google Health data type id
# and the filter field path used to window the read. A stream absent here is
# unsupported.
STREAM_DATA_TYPES = {
    "sleep": "com.google.sleep",
}
STREAM_FILTER_FIELDS = {
    "sleep": "sleep.interval.start_time",
}


class GoogleHealthFetchClient:
    """Fetch Google Health v4 data-point pages for a stream and time window."""

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
        return f"GoogleHealthFetchClient(provider={PROVIDER!r})"

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
        """Fetch one page of ``stream`` for the given time window.

        The list call windows the read with an AIP-160 ``filter`` over the data
        type's interval start time; a ``pageToken`` advances through pages, and
        the response carries a ``nextPageToken`` until the window is exhausted.
        """
        if not access_token:
            msg = "access_token must be non-empty"
            raise ValueError(msg)
        data_type = STREAM_DATA_TYPES.get(stream)
        filter_field = STREAM_FILTER_FIELDS.get(stream)
        if data_type is None or filter_field is None:
            msg = f"unsupported Google Health stream {stream!r}"
            raise ValueError(msg)

        start = require_aware(window_start, field_name="window_start")
        end = require_aware(window_end, field_name="window_end")
        if end < start:
            msg = "window_end must not precede window_start"
            raise ValueError(msg)

        start_s = to_utc_rfc3339(start)
        end_s = to_utc_rfc3339(end)
        # Half-open [start, end) window over the interval start time.
        filter_expr = f'{filter_field} >= "{start_s}" AND {filter_field} < "{end_s}"'
        params: dict[str, str] = {
            "filter": filter_expr,
            "pageSize": str(SLEEP_PAGE_SIZE),
        }
        if page_token:
            params["pageToken"] = page_token

        url = f"{self._api_base}/users/me/dataTypes/{data_type}/dataPoints"
        try:
            response = self._send(url, params, access_token)
        except httpx2.HTTPError:
            # The exception text can echo the request, which carries the token.
            logger.warning("google_health fetch transport error", extra={"stream": stream})
            return FetchResult(failure=FetchFailure.TRANSPORT_ERROR)

        if response.status_code >= 400:
            return self._classify_error(response, stream=stream)

        text = response.text
        try:
            parsed = response.json()
        except ValueError:
            logger.warning(
                "google_health fetch response was not valid json",
                extra={"stream": stream, "status": response.status_code},
            )
            return FetchResult(failure=FetchFailure.MALFORMED_RESPONSE)

        next_token = None
        if isinstance(parsed, dict):
            raw_next = parsed.get("nextPageToken")
            if isinstance(raw_next, str) and raw_next:
                next_token = raw_next

        return FetchResult(
            envelope=RawEnvelope(
                provider=PROVIDER,
                stream=stream,
                payload_text=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                http_status=response.status_code,
                content_type=response.headers.get("content-type"),
                fetched_at=to_utc_rfc3339(require_aware(now, field_name="now")),
                # Redacted: a path template and window bounds, never the token.
                request_meta={
                    "url_template": f"v4/users/me/dataTypes/{data_type}/dataPoints",
                    "start_time": start_s,
                    "end_time": end_s,
                },
                page_token=page_token,
                next_page_token=next_token,
            )
        )

    def _send(self, url: str, params: dict[str, str], access_token: str) -> httpx2.Response:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if self._transport is not None:
            return self._transport.get(url, params=params, headers=headers, timeout=self._timeout)
        with httpx2.Client(timeout=self._timeout) as client:
            return client.get(url, params=params, headers=headers)

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
            "google_health fetch rejected",
            extra={
                "stream": stream,
                "status": status,
                "failure": str(failure),
                "retry_after_seconds": retry_after,
            },
        )
        return FetchResult(failure=failure, retry_after_seconds=retry_after)


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` delta-seconds header, ignoring HTTP-date form."""
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return max(seconds, 0)
