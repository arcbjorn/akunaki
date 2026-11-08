"""Entrypoint: python -m akunaki.api"""

from __future__ import annotations

import logging

import uvicorn

from akunaki.api.app import create_app
from akunaki.api.logfilters import uvicorn_log_config
from akunaki.api.request_context import RequestIdFilter
from akunaki.config import get_settings
from akunaki.logfmt import JsonFormatter


def configure_logging() -> None:
    """Install structured logging with the request-id filter on every record."""
    handler = logging.StreamHandler()
    # The request-id filter stamps ``request_id`` on the record; the formatter
    # renders it like any other extra field, "-" outside a request.
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # ``/readyz`` compares the schema to the migration head on every probe, and
    # alembic announces its dialect at INFO each time it does — two lines per
    # probe, tens of thousands a day, carrying nothing; its plugin registry
    # adds seven more at import. Migrations themselves run in the init
    # container, not here, so nothing alembic says at INFO matters here.
    logging.getLogger("alembic").setLevel(logging.WARNING)


def main() -> None:
    """Run the API with uvicorn; bind address comes from settings."""
    configure_logging()
    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        log_config=uvicorn_log_config(),
    )


if __name__ == "__main__":
    main()
