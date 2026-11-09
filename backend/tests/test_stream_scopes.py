"""Scope gating: a stream whose scope the grant lacks is skipped, not attempted."""

from __future__ import annotations

from akunaki.application.sync_handlers import (
    GOOGLE_HEALTH_METRICS_SCOPE,
    stream_is_granted,
    stream_required_scope,
)

_OLD_GOOGLE_GRANT = (
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
)


def test_google_vitals_streams_need_the_metrics_scope() -> None:
    for stream in ("resting_heart_rate", "heart_rate_variability"):
        assert stream_required_scope("google_health", stream) == GOOGLE_HEALTH_METRICS_SCOPE
        assert stream_is_granted("google_health", stream, _OLD_GOOGLE_GRANT) is False
        assert stream_is_granted(
            "google_health", stream, (*_OLD_GOOGLE_GRANT, GOOGLE_HEALTH_METRICS_SCOPE)
        )


def test_google_sleep_and_activity_are_not_gated() -> None:
    for stream in ("sleep", "steps", "active_minutes"):
        assert stream_required_scope("google_health", stream) is None
        assert stream_is_granted("google_health", stream, _OLD_GOOGLE_GRANT)


def test_a_grant_that_recorded_no_scopes_is_trusted() -> None:
    assert stream_is_granted("google_health", "heart_rate_variability", None)
    assert stream_is_granted("google_health", "heart_rate_variability", ())


def test_oura_gate_still_strips_the_vendor_prefix() -> None:
    assert stream_is_granted("oura", "workout", ("extapi:daily", "extapi:workout"))
    assert stream_is_granted("oura", "workout", ("extapi:daily",)) is False
