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
from datetime import datetime

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

        # Oura V2 collections filter on local dates, not instants.
        params: dict[str, str] = {
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
        }
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
                # Redacted: a path template and date bounds, never the token.
                request_meta={
                    "url_template": f"v2/usercollection/{path}",
                    "start_date": params["start_date"],
                    "end_date": params["end_date"],
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
        else:
            # 5xx is transient; other 4xx is a request-shape bug on our side.
            # Both are non-auth, so neither should flip the connection to
            # needs_reauth — the job's retry budget bounds them instead.
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
