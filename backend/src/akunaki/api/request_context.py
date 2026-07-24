"""Per-request correlation id: middleware, context, and a logging filter.

Every request gets a **request id** — an inbound ``X-Request-ID`` when a trusted
upstream (a load balancer) supplies a well-formed one, otherwise a fresh UUID.
The id is bound to a ``ContextVar`` for the duration of the request so any log
record emitted while handling it carries the id, and it is echoed on the
response ``X-Request-ID`` header so a client (and the access log) can correlate.

A ``ContextVar`` is the right carrier: it is isolated per async task, so
concurrent requests never see each other's id, and it needs no threading of an
argument through every layer.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

# The bound id for the current request, or "-" outside any request (e.g. a log
# emitted at startup). "-" is a safe, greppable sentinel for "no request".
_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Accept an inbound id only when it is a bounded, safe token: this prevents a
# client from injecting newlines or huge values into our logs via the header.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def current_request_id() -> str:
    """Return the request id bound to the current context, or ``-``."""
    return _request_id.get()


def _resolve_id(inbound: str | None) -> str:
    """Use a trusted inbound id when it is well-formed, else mint a fresh one."""
    if inbound is not None and _SAFE_ID.match(inbound):
        return inbound
    return str(uuid.uuid4())


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind a request id for the call and echo it on the response header."""
    request_id = _resolve_id(request.headers.get(REQUEST_ID_HEADER))
    token = _request_id.set(request_id)
    try:
        response = await call_next(request)
    finally:
        _request_id.reset(token)
    # Set unconditionally: the id is ours to report, even if a handler set one.
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


class RequestIdFilter(logging.Filter):
    """Inject the current request id onto every log record as ``request_id``.

    A filter (not a formatter) so the field is available to any formatter,
    including the JSON access log, without each call site passing it via
    ``extra``. Outside a request the id is ``-``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True
