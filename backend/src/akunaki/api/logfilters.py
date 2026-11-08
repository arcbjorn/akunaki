"""Log filters for the API process.

The kubelet hits ``/healthz`` and ``/readyz`` every few seconds, and uvicorn
wrote one access line per hit: three quarters of the API's log volume — some
280,000 lines a week in the log store — saying nothing. A probe that fails is
a different matter, so only *successful* probe hits are dropped.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import uvicorn.config

_PROBE_PATHS = frozenset({"/healthz", "/readyz"})


class ProbeAccessFilter(logging.Filter):
    """Drop uvicorn access records for 2xx liveness and readiness probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep everything except a successful probe hit."""
        # uvicorn logs access as
        #   '%s - "%s %s HTTP/%s" %d', client, method, path, version, status
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        return not (path.split("?", 1)[0] in _PROBE_PATHS and 200 <= status < 300)


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging config with the probe filter on the access handler.

    Passed to ``uvicorn.run`` so the filter is installed by uvicorn's own
    ``dictConfig`` rather than bolted on before it, which would lose the race
    against that configuration.
    """
    config: dict[str, Any] = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config.setdefault("filters", {})["probe_access"] = {"()": f"{__name__}.ProbeAccessFilter"}
    config["handlers"]["access"].setdefault("filters", []).append("probe_access")
    return config
