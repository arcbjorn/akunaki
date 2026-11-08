"""The API's probe filter: successful liveness/readiness hits leave no access line.

kubelet probes every few seconds and uvicorn wrote one line per hit — three
quarters of the API's log volume, saying nothing. A probe that *fails* is the
line an operator needs, so only 2xx probe hits are dropped.
"""

from __future__ import annotations

import logging
import logging.config

from akunaki.api.logfilters import ProbeAccessFilter, uvicorn_log_config


def _access_record(path: str, status: int) -> logging.LogRecord:
    # uvicorn: '%s - "%s %s HTTP/%s" %d', client, method, path, version, status
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.42.0.1:1234", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_successful_probe_hits_are_dropped() -> None:
    flt = ProbeAccessFilter()
    assert flt.filter(_access_record("/healthz", 200)) is False
    assert flt.filter(_access_record("/readyz", 200)) is False
    assert flt.filter(_access_record("/readyz?verbose=1", 204)) is False


def test_failing_probes_and_real_requests_are_kept() -> None:
    flt = ProbeAccessFilter()
    assert flt.filter(_access_record("/readyz", 503)) is True
    assert flt.filter(_access_record("/healthz", 500)) is True
    assert flt.filter(_access_record("/v1/connections", 200)) is True
    assert flt.filter(_access_record("/healthz-and-more", 200)) is True


def test_records_of_another_shape_pass_through() -> None:
    flt = ProbeAccessFilter()
    record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, "started", None, None)
    assert flt.filter(record) is True
    odd = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s", ("x",), None)
    assert flt.filter(odd) is True


def test_uvicorn_config_installs_the_filter_on_the_access_handler() -> None:
    """A typo in the factory path would crash the API at boot; dictConfig must accept it."""
    config = uvicorn_log_config()

    assert "probe_access" in config["handlers"]["access"]["filters"]
    assert config["filters"]["probe_access"]["()"].endswith("ProbeAccessFilter")
    logging.config.dictConfig(config)
    handlers = logging.getLogger("uvicorn.access").handlers
    assert any(isinstance(f, ProbeAccessFilter) for h in handlers for f in h.filters)
