"""Prometheus-format metrics exposition.

Serves the process-wide registry in the Prometheus text format, which an
OpenTelemetry collector scrapes just as readily — satisfying the metrics table
in operations.md without adding a client library to a core install that CI
proves boots on a minimal dependency set.

**Unauthenticated, and deliberately so** — a scraper is infrastructure, not a
logged-in user, and cookie auth would make it unscrapeable. That is only safe
because the exposition is PHI-free by construction: counts and liveness gauges,
labelled with bounded tokens (provider, outcome class), never a tenant id, a
user id, or a health value. Expose the port to the scraper, not the internet.

The API process reports its own counters; the worker process has its own
registry and is observed through the queue/leader fields on ``/readyz``. Two
processes cannot share an in-memory registry, and inventing a shared one would
mean either a metrics store or a push gateway — both beyond what the design
calls for at this stage.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from akunaki.application.metrics import CONTENT_TYPE, REGISTRY

router = APIRouter(tags=["health"])


@router.get(
    "/metrics",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}, "description": "Prometheus exposition."}},
)
def metrics() -> Response:
    """Render the process registry in the Prometheus text format."""
    return Response(
        content=REGISTRY.render(),
        media_type=CONTENT_TYPE,
        # A scrape must never be served from a cache: a stale sample would
        # misreport liveness.
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
