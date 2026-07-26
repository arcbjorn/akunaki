"""Polar AccessLink data fetch client.

Returns the **exact** vendor body so the transport layer can persist a faithful
record; nothing here reinterprets or reshapes vendor data. Mirrors the Oura
client's secrets discipline: the access token rides in the Authorization header
and is never logged, and response bodies never reach log records or exceptions.

Reads the **non-transactional** exercises resource::

    GET /v3/exercises?zones=true

One call returns the array of exercises with their HR-zone durations already
inlined under ``heart_rate_zones`` (the ``zones=true`` query parameter), so no
per-exercise or per-zone follow-up request is needed. The response is a bare
JSON array; it is stored exactly as received.

AccessLink also exposes a deprecated ``exercise-transactions`` lifecycle
(open → list → fetch each → commit). It is **not** used: the vendor marks those
paths "Exercises (deprecated)" and directs partners to this resource.

Two consequences of the non-transactional model, both handled by the caller:

- **No commit step.** The vendor does not track what was delivered, so the same
  exercises are returned on every call. Re-delivery is absorbed by content-hash
  dedupe in the ingestion layer, which makes a repeat sync a no-op.
- **A 30-day retention horizon.** Only exercises uploaded to Flow in the last
  30 days are returned at all, and only those uploaded after the user linked
  this client.

Only the ``workout`` stream is supported. The resource is unpaginated, so
``next_page_token`` is always None.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import httpx2

from akunaki.domain.fetch import FetchFailure, FetchResult, RawEnvelope
from akunaki.domain.jobs import require_aware, to_utc_rfc3339

logger = logging.getLogger("akunaki.connectors.polar_fetch")

PROVIDER = "polar"
API_BASE = "https://www.polaraccesslink.com/v3"
DEFAULT_TIMEOUT_SECONDS = 30.0

# Streams this connector can fetch, mapped to their AccessLink resource path.
# A stream absent here is unsupported.
STREAM_PATHS = {
    "workout": "exercises",
}


class PolarFetchClient:
    """Fetch Polar AccessLink exercises from the non-transactional resource."""

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
        """Fetch the available exercises as one faithful page.

        The resource takes no date filter — it returns whatever is inside the
        vendor's own 30-day retention horizon — so the window bounds are
        validated (for a uniform connector contract) but not sent. There is no
        pagination, so there is never a next-page token.
        """
        if not access_token:
            msg = "access_token must be non-empty"
            raise ValueError(msg)
        path = STREAM_PATHS.get(stream)
        if path is None:
            msg = f"unsupported Polar stream {stream!r}"
            raise ValueError(msg)

        start = require_aware(window_start, field_name="window_start")
        end = require_aware(window_end, field_name="window_end")
        if end < start:
            msg = "window_end must not precede window_start"
            raise ValueError(msg)

        url = f"{self._api_base}/{path}"
        # `zones=true` inlines each exercise's HR-zone durations, which is what
        # the canonical zone-load computation needs; without it the exercises
        # come back without `heart_rate_zones` and normalize to nothing.
        params = {"zones": "true"}
        try:
            response = self._send(url, params, access_token)
        except httpx2.HTTPError:
            # The exception text can echo the request, which carries the token.
            logger.warning("polar fetch transport error", extra={"stream": stream})
            return FetchResult(failure=FetchFailure.TRANSPORT_ERROR)

        if response.status_code >= 400:
            return self._classify_error(response, stream=stream)

        body = response.text
        try:
            response.json()
        except ValueError:
            logger.warning(
                "polar fetch response was not valid json",
                extra={"stream": stream, "status": response.status_code},
            )
            return FetchResult(failure=FetchFailure.MALFORMED_RESPONSE)

        return FetchResult(
            envelope=RawEnvelope(
                provider=PROVIDER,
                stream=stream,
                payload_text=body,
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                http_status=response.status_code,
                content_type=response.headers.get("content-type"),
                fetched_at=to_utc_rfc3339(require_aware(now, field_name="now")),
                # Redacted: a path template only, never the token.
                request_meta={"url_template": f"v3/{path}", "zones": "true"},
                page_token=None,
                next_page_token=None,
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
        """Map a non-2xx response to a typed failure. The body is never logged."""
        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("retry-after"))

        if status in (401, 403):
            # 403 here is documented as "user has not accepted all mandatory
            # consents", which re-authorization is exactly the remedy for.
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


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` delta-seconds header, ignoring HTTP-date form."""
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return max(seconds, 0)
