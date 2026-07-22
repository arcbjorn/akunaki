"""Security response headers for the JSON API.

This is a JSON-only API — it never returns HTML a browser would render — so the
Content-Security-Policy is maximally restrictive (``default-src 'none'``): even
if a response body were somehow interpreted as a document, it could load
nothing. The other headers close common browser-side footguns (MIME sniffing,
framing, referrer leakage).

These apply to **every** response, including errors, so they cannot be forgotten
per route. CORS is handled separately by FastAPI's ``CORSMiddleware`` from the
configured allow-list.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

# A JSON API renders no document; deny every resource type by default.
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


async def security_headers_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Attach the security headers to every response, including errors."""
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        # Do not clobber a header a handler set deliberately.
        response.headers.setdefault(name, value)
    return response
