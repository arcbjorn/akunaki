"""Oura V2 data fetch client.

Returns the **exact** response body so the transport layer can persist a
faithful record; nothing here reinterprets or reshapes vendor data.

Secrets discipline matches the OAuth client: the access token is sent in the
Authorization header and never logged, and response bodies are never attached
to log records or exceptions.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

import httpx2

from akunaki.domain.fetch import FetchFailure, FetchResult, RawEnvelope
from akunaki.domain.jobs import require_aware, to_utc_rfc3339

logger = logging.getLogger("akunaki.connectors.oura_fetch")

PROVIDER = "oura"
API_BASE = "https://api.ouraring.com/v2/usercollection"
DEFAULT_TIMEOUT_SECONDS = 30.0

# Streams this connector can fetch, mapped to their V2 path segment.
STREAM_PATHS = {
    "sleep": "sleep",
    "daily_sleep": "daily_sleep",
    "daily_readiness": "daily_readiness",
    "daily_activity": "daily_activity",
    "workout": "workout",
    # Retained-only streams: fetched and kept as immutable raw payloads with
    # full lineage, but not normalized into facts. Several carry a vendor 0-100
    # score (readiness, sleep) that v0.1.0 deliberately does not surface — the
    # product ships exactly one score, and publishing a second would imply a
    # formula nobody accepted. Retaining them now means a normalizer added later
    # can be re-run over history rather than starting from the day it shipped.
    "daily_stress": "daily_stress",
    "daily_cardiovascular_age": "daily_cardiovascular_age",
    "daily_resilience": "daily_resilience",
    "vo2_max": "vO2_max",
    "sleep_time": "sleep_time",
    "session": "session",
    "heartrate": "heartrate",
}

# Streams whose window is expressed as **instants**, not local dates. Oura's
# collections filter on `start_date`/`end_date`, but the heart-rate series is
# sampled continuously and takes `start_datetime`/`end_datetime`; sending dates
# there returns a 422 rather than a day's samples.
DATETIME_WINDOW_STREAMS = frozenset({"heartrate"})

# Vendor **maximum** window width per request, in days. Oura refuses a
# heart-rate range wider than 30 days ("Timerange between start and endtime has
# to be less than or equal to 30 days") — and the default backfill asks for 30
# days *plus* a 36-hour overlap, which is just over the line. Clamping costs the
# oldest hours of one call, which the next sync's own window covers; not
# clamping means the stream never ingests anything at all.
STREAM_MAX_WINDOW_DAYS = {
    "heartrate": 30,
}


class OuraFetchClient:
    """Fetch Oura V2 collection pages for a stream and date window."""

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
        return f"OuraFetchClient(provider={PROVIDER!r})"

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
        """Fetch one page of ``stream`` for the given window."""
        if not access_token:
            msg = "access_token must be non-empty"
            raise ValueError(msg)
        path = STREAM_PATHS.get(stream)
        if path is None:
            msg = f"unsupported Oura stream {stream!r}"
            raise ValueError(msg)

        start = require_aware(window_start, field_name="window_start")
        end = require_aware(window_end, field_name="window_end")
        if end < start:
            msg = "window_end must not precede window_start"
            raise ValueError(msg)

        # Clamp to the vendor's ceiling for this stream before building the
        # window, so a too-wide lookback trims rather than being refused whole.
        max_days = STREAM_MAX_WINDOW_DAYS.get(stream)
        if max_days is not None:
            start = max(start, end - timedelta(days=max_days))

        # Oura V2 collections filter on local dates, not instants — except the
        # continuously-sampled series, which take instants and reject dates.
        params: dict[str, str] = (
            {
                "start_datetime": to_utc_rfc3339(start),
                "end_datetime": to_utc_rfc3339(end),
            }
            if stream in DATETIME_WINDOW_STREAMS
            else {
                "start_date": start.date().isoformat(),
                "end_date": end.date().isoformat(),
            }
        )
        if page_token:
            params["next_token"] = page_token

        url = f"{self._api_base}/{path}"
        try:
            response = self._send(url, params, access_token)
        except httpx2.HTTPError:
            # The exception text can echo the request, which carries the token.
            logger.warning(
                "oura fetch transport error",
                extra={"stream": stream},
            )
            return FetchResult(failure=FetchFailure.TRANSPORT_ERROR)

        if response.status_code >= 400:
            return self._classify_error(response, stream=stream)

        body = response.text
        try:
            parsed = response.json()
        except ValueError:
            logger.warning(
                "oura fetch response was not valid json",
                extra={"stream": stream, "status": response.status_code},
            )
            return FetchResult(failure=FetchFailure.MALFORMED_RESPONSE)

        next_token = None
        if isinstance(parsed, dict):
            raw_next = parsed.get("next_token")
            if isinstance(raw_next, str) and raw_next:
                next_token = raw_next

        return FetchResult(
            envelope=RawEnvelope(
                provider=PROVIDER,
                stream=stream,
                payload_text=body,
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                http_status=response.status_code,
                content_type=response.headers.get("content-type"),
                fetched_at=to_utc_rfc3339(require_aware(now, field_name="now")),
                # Redacted: a path template and the window bounds actually
                # sent, never the token. Echoing the real parameter names keeps
                # the retained record honest about how the window was expressed,
                # which differs between the date and datetime collections.
                request_meta={
                    "url_template": f"v2/usercollection/{path}",
                    **{key: value for key, value in params.items() if key != "next_token"},
                },
                page_token=page_token,
                next_page_token=next_token,
            )
        )

    def _send(
        self,
        url: str,
        params: dict[str, str],
        access_token: str,
    ) -> httpx2.Response:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if self._transport is not None:
            return self._transport.get(url, params=params, headers=headers, timeout=self._timeout)
        with httpx2.Client(timeout=self._timeout) as client:
            return client.get(url, params=params, headers=headers)

    def _classify_error(self, response: httpx2.Response, *, stream: str) -> FetchResult:
        """Map a non-2xx response to a typed failure.

        The body is never logged: an error body can echo request context.
        """
        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("retry-after"))

        if status in (401, 403):
            failure = FetchFailure.UNAUTHORIZED
        elif status == 429:
            failure = FetchFailure.RATE_LIMIT
        elif 400 <= status < 500:
            # A 4xx is a request-shape bug on our side: the same request will be
            # refused every time, so retrying only burns the attempt budget and
            # buries the cause under a generic "transient" label. Reported as
            # malformed — permanent, and named — so a wrong window or parameter
            # surfaces on the first attempt instead of after the budget is gone.
            failure = FetchFailure.MALFORMED_REQUEST
        else:
            # 5xx: the vendor is unwell, and a retry may well succeed. Non-auth,
            # so it must not flip the connection to needs_reauth.
            failure = FetchFailure.PROVIDER_ERROR

        logger.warning(
            "oura fetch rejected",
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
        # HTTP-date form is valid but unused by this provider; ignore rather
        # than guess a delay.
        return None
    return max(seconds, 0)
