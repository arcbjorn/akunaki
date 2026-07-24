"""Tests for the request-id context, filter, and middleware."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from akunaki.api.app import create_app
from akunaki.api.request_context import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    current_request_id,
)
from akunaki.config import Settings

_MEMORY = "sqlite+libsql:///:memory:"


def _client() -> TestClient:
    return TestClient(create_app(Settings(database_url=_MEMORY)))


def test_generates_a_request_id_when_none_supplied() -> None:
    resp = _client().get("/healthz")
    rid = resp.headers.get(REQUEST_ID_HEADER)
    assert rid is not None
    # A fresh UUID (36 chars with hyphens).
    assert len(rid) == 36 and rid.count("-") == 4


def test_echoes_a_trusted_inbound_id() -> None:
    resp = _client().get("/healthz", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_rejects_an_unsafe_inbound_id() -> None:
    # A value with spaces / control chars must not be reflected — it could
    # inject into logs — so a fresh id is minted instead.
    resp = _client().get("/healthz", headers={REQUEST_ID_HEADER: "bad id\nwith newline"})
    assert resp.headers[REQUEST_ID_HEADER] != "bad id\nwith newline"
    assert len(resp.headers[REQUEST_ID_HEADER]) == 36


def test_overlong_inbound_id_is_rejected() -> None:
    resp = _client().get("/healthz", headers={REQUEST_ID_HEADER: "a" * 200})
    assert resp.headers[REQUEST_ID_HEADER] != "a" * 200


def test_ids_differ_across_requests() -> None:
    client = _client()
    a = client.get("/healthz").headers[REQUEST_ID_HEADER]
    b = client.get("/healthz").headers[REQUEST_ID_HEADER]
    assert a != b


def test_id_is_unbound_outside_a_request() -> None:
    # After the request completes the context is reset to the sentinel.
    _client().get("/healthz")
    assert current_request_id() == "-"


def test_filter_injects_request_id_onto_records() -> None:
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0, msg="m", args=(), exc_info=None
    )
    assert RequestIdFilter().filter(record) is True
    # Outside a request the sentinel is set (never missing, so a formatter that
    # references request_id never raises).
    assert record.request_id == "-"  # type: ignore[attr-defined]
