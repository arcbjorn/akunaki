"""Metrics registry, exposition format, and the /metrics endpoint.

Covers what a scraper depends on: counters that only rise, gauges that track
the latest value, a text rendering that parses, and label discipline that keeps
tenant ids and health values out of series names.
"""

from __future__ import annotations

import threading
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from akunaki.api.app import create_app
from akunaki.application.metrics import (
    CONTENT_TYPE,
    REGISTRY,
    MetricError,
    MetricsRegistry,
)
from akunaki.config import Settings, clear_settings_cache


@pytest.fixture(autouse=True)
def _reset_registry() -> Generator[None]:
    """Keep the process-wide registry from leaking counts across tests."""
    REGISTRY.reset()
    yield
    REGISTRY.reset()


def test_counter_accumulates_and_gauge_tracks_latest() -> None:
    registry = MetricsRegistry()
    hits = registry.counter("hits_total", "Requests served.")
    live = registry.gauge("live", "Loop running.")

    hits.inc()
    hits.inc(3)
    live.set(1)
    live.set(0)

    assert hits.value() == 4
    assert live.value() == 0


def test_counter_rejects_a_negative_amount() -> None:
    registry = MetricsRegistry()
    hits = registry.counter("hits_total", "Requests served.")

    with pytest.raises(MetricError):
        hits.inc(-1)


def test_labelled_series_are_tracked_independently() -> None:
    registry = MetricsRegistry()
    fetches = registry.counter("fetch_total", "Fetches.", labels=("provider", "result"))

    fetches.inc(provider="oura", result="ok")
    fetches.inc(provider="oura", result="ok")
    fetches.inc(provider="polar", result="rate_limit")

    assert fetches.value(provider="oura", result="ok") == 2
    assert fetches.value(provider="polar", result="rate_limit") == 1
    # A series never touched reads zero rather than raising.
    assert fetches.value(provider="polar", result="ok") == 0


def test_wrong_label_set_is_rejected() -> None:
    """A typo'd label would otherwise create a silent parallel series."""
    registry = MetricsRegistry()
    fetches = registry.counter("fetch_total", "Fetches.", labels=("provider",))

    with pytest.raises(MetricError):
        fetches.inc(provider="oura", extra="x")
    with pytest.raises(MetricError):
        fetches.inc()


def test_unbounded_label_value_is_rejected() -> None:
    """Guards the PHI/cardinality rule at the point of use."""
    registry = MetricsRegistry()
    fetches = registry.counter("fetch_total", "Fetches.", labels=("provider",))

    with pytest.raises(MetricError):
        fetches.inc(provider="x" * 65)


def test_re_registration_with_a_different_shape_is_rejected() -> None:
    registry = MetricsRegistry()
    registry.counter("thing_total", "A thing.", labels=("a",))

    # Same name, same shape: the existing family is returned.
    assert registry.counter("thing_total", "A thing.", labels=("a",)) is not None
    with pytest.raises(MetricError):
        registry.counter("thing_total", "A thing.", labels=("b",))
    with pytest.raises(MetricError):
        registry.gauge("thing_total", "A thing.", labels=("a",))


def test_render_emits_prometheus_text_format() -> None:
    registry = MetricsRegistry()
    registry.counter("jobs_total", "Jobs settled.", labels=("disposition",)).inc(
        2, disposition="succeeded"
    )
    registry.gauge("live", "Loop running.").set(1)

    text = registry.render()

    assert "# HELP jobs_total Jobs settled." in text
    assert "# TYPE jobs_total counter" in text
    assert 'jobs_total{disposition="succeeded"} 2' in text
    assert "# TYPE live gauge" in text
    assert "live 1" in text
    assert text.endswith("\n")


def test_render_emits_zero_for_an_untouched_unlabelled_family() -> None:
    """ "No failures yet" and "cannot fail" must not look identical."""
    registry = MetricsRegistry()
    registry.counter("dead_letters_total", "Dead letters.")

    text = registry.render()

    assert "# TYPE dead_letters_total counter" in text
    assert "dead_letters_total 0" in text


def test_render_escapes_label_values() -> None:
    registry = MetricsRegistry()
    registry.counter("thing_total", "A thing.", labels=("name",)).inc(name='a"b\\c')

    assert r'thing_total{name="a\"b\\c"} 1' in registry.render()


def test_concurrent_increments_do_not_lose_counts() -> None:
    """The worker heartbeats on a background thread while requests record."""
    registry = MetricsRegistry()
    hits = registry.counter("hits_total", "Hits.")
    barrier = threading.Barrier(8)

    def bump() -> None:
        barrier.wait(timeout=10)
        for _ in range(500):
            hits.inc()

    threads = [threading.Thread(target=bump, name=f"bump-{i}") for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert hits.value() == 8 * 500


def test_metrics_endpoint_is_absent_unless_enabled() -> None:
    """Unauthenticated surfaces are not registered on an opted-out deployment."""
    clear_settings_cache()
    app = create_app(Settings(database_url="sqlite+libsql://"))
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404
    assert "/metrics" not in app.openapi()["paths"]


def test_metrics_endpoint_serves_prometheus_text_when_enabled() -> None:
    clear_settings_cache()
    app = create_app(Settings(database_url="sqlite+libsql://", metrics_enabled=True))
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    # A scrape must not be cached: a stale sample misreports liveness.
    assert response.headers["cache-control"] == "no-store"
    # The declared families are present even before anything has happened.
    assert "# TYPE akunaki_jobs_dead_letters_total counter" in response.text
    assert "# TYPE akunaki_worker_liveness gauge" in response.text
