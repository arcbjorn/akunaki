"""Google Health API v4 data fetch client.

Returns the **exact** response body so the transport layer can persist a
faithful record; nothing here reinterprets or reshapes vendor data. Mirrors the
Oura and Polar clients' secrets discipline: the access token rides in the
Authorization header and is never logged, and response bodies never reach log
records or exceptions.

Two read shapes, chosen per stream by how the vendor reports that data type:

- ``users.dataTypes.dataPoints.list`` (a **GET**) for ``sleep``, windowed by an
  AIP-160 ``filter`` over the data type's interval and paginated by
  ``nextPageToken``.
- ``dataPoints:dailyRollUp`` (a **POST**) for ``steps`` and ``active-minutes``,
  which v4 reports per **minute**. Listing those would spread a single day over
  several pages, and each page would become its own version of that day's fact —
  the last one written replacing the day's total instead of adding to it. The
  roll-up returns one aggregated point per local day, so one payload is one
  complete day, which is what the versioned fact write assumes.

Google Health is the Fitbit-origin source: cloud sleep plus the daytime activity
the design pairs against Polar workouts for overlap exclusion.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

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
#
# The v4 data type id is the bare name (`sleep`), kebab-cased in a path when it
# has more than one word (`body-fat`) and snake-cased in a filter (`body_fat`).
# It is *not* the legacy Google Fit `com.google.*` namespace, which v4 rejects.
STREAM_DATA_TYPES = {
    "sleep": "sleep",
    "steps": "steps",
    "active_minutes": "active-minutes",
}
# The filterable member for each stream's window.
#
# Sleep filters on the session's **end** time, not its start. This is a v4
# constraint, not a preference: the reference states only end-time filtering is
# supported for sleep sessions, and the API rejects `sleep.interval.start_time`
# outright with `INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER` ("Member ... is not
# supported for filtering"). Verified against the live API 2026-08-13.
#
# It also happens to be the correct semantic choice: a night is assigned to the
# local date it **ended** (the wake-date rule the sleep normalizer applies), so
# windowing on end time selects exactly the nights that belong to the window.
# Sessions and intervals filter on **opposite** ends, and v4 rejects the wrong
# one outright, so each stream's member is verified against the live API rather
# than inferred: `sleep` (a session) accepts only `end_time`, while `steps` and
# `active-minutes` (intervals) accept only `start_time`. Verified 2026-08-13.
STREAM_FILTER_FIELDS = {
    "sleep": "sleep.interval.end_time",
    "steps": "steps.interval.start_time",
    "active_minutes": "active_minutes.interval.start_time",
}

# Streams read through `dataPoints:dailyRollUp` (a POST) rather than the
# `dataPoints` list GET. These data types are reported per **minute**, so the
# list form spreads one day across many pages; the roll-up returns one
# aggregated point per local day, which is the unit a daily fact represents.
ROLLUP_STREAMS = frozenset({"steps", "active_minutes"})

# Vendor **maximum** window per request, in days. v4 caps the range for
# `active-minutes` (with heart-rate, total-calories, and
# calories-in-heart-rate-zone) at 14 days and everything else at 90; a wider
# request is refused outright with a bare "Invalid argument in request", which
# the transport classifies as transient and would retry forever. A stream absent
# here has no cap beyond the caller's window.
#
# Note this is the *opposite* constraint to `GOOGLE_HEALTH_MIN_WINDOW` (a 14-day
# **floor** on the sleep list call): for `active-minutes`, 14 days is the ceiling.
STREAM_MAX_WINDOW_DAYS = {
    "active_minutes": 14,
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
        rollup = stream in ROLLUP_STREAMS
        if rollup:
            # Daily roll-up: one already-aggregated point per local day. The
            # `dataPoints.list` alternative returns **per-minute** intervals, so
            # a day arrives spread over several pages — and since each page
            # becomes its own version of that day's fact, the last page written
            # would replace the day's total instead of adding to it. Rolling up
            # vendor-side keeps one payload equal to one complete day.
            url = f"{url}:dailyRollUp"
            # Clamp to the vendor's ceiling for this data type. Trimming the
            # window costs older days on one call — which the next sync's own
            # window covers — whereas exceeding it has the whole request
            # refused, so the stream would ingest nothing at all.
            max_days = STREAM_MAX_WINDOW_DAYS.get(stream)
            if max_days is not None:
                start = max(start, end - timedelta(days=max_days))
        try:
            response = (
                self._post(url, _rollup_body(start, end, page_token), access_token)
                if rollup
                else self._send(url, params, access_token)
            )
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
                    "url_template": (
                        f"v4/users/me/dataTypes/{data_type}/dataPoints"
                        + (":dailyRollUp" if rollup else "")
                    ),
                    "start_time": start_s,
                    "end_time": end_s,
                },
                page_token=page_token,
                next_page_token=next_token,
            )
        )

    def _post(
        self,
        url: str,
        body: dict[str, object],
        access_token: str,
    ) -> httpx2.Response:
        """POST a roll-up request (the aggregate methods are POST, not GET)."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._transport is not None:
            return self._transport.post(url, json=body, headers=headers, timeout=self._timeout)
        with httpx2.Client(timeout=self._timeout) as client:
            return client.post(url, json=body, headers=headers)

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


def _rollup_body(
    start: datetime,
    end: datetime,
    page_token: str | None,
) -> dict[str, object]:
    """Build a ``dataPoints:dailyRollUp`` request body.

    The range is a **civil** (timezone-free) date interval: the vendor resolves
    the day boundary in the user's own timezone, which is exactly the boundary
    the resulting facts are keyed by, so no offset is applied here.
    """
    body: dict[str, object] = {
        "range": {
            "start": {"date": _civil_date(start)},
            "end": {"date": _civil_date(end)},
        }
    }
    if page_token:
        body["pageToken"] = page_token
    return body


def _civil_date(value: datetime) -> dict[str, int]:
    return {"year": value.year, "month": value.month, "day": value.day}
