"""Tests for the per-provider backfill config factory.

The initial-sync handler is provider-agnostic; this factory is the single place
that pairs a provider with the stream and schema version it backfills. These
verify the supported providers, the loud failure for an unwired one, and that
policy knobs pass through.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from akunaki.application.sync_handlers import (
    DEFAULT_LOOKBACK_DAYS,
    sync_config_for_provider,
)


def test_oura_backfills_the_sleep_stream() -> None:
    config = sync_config_for_provider("oura")
    # Oura's sleep payload carries both sleep and the overnight vitals.
    assert config.stream == "sleep"
    assert config.schema_version == "oura.v2"
    assert config.lookback_days == DEFAULT_LOOKBACK_DAYS


def test_polar_backfills_the_workout_stream() -> None:
    config = sync_config_for_provider("polar")
    # Polar's exercises list normalizes to canonical workouts; the normalize
    # dispatch keys on the ``polar.`` schema prefix.
    assert config.stream == "workout"
    assert config.schema_version == "polar.v1"
    assert config.schema_version.startswith("polar.")


def test_unwired_provider_fails_loudly() -> None:
    # An unknown provider is never a silent Oura fallback.
    with pytest.raises(ValueError, match="no backfill config for provider 'garmin'"):
        sync_config_for_provider("garmin")


def test_policy_knobs_pass_through() -> None:
    config = sync_config_for_provider(
        "polar",
        lookback_days=7,
        overlap=timedelta(hours=12),
        max_pages=3,
    )
    assert config.lookback_days == 7
    assert config.overlap == timedelta(hours=12)
    assert config.max_pages == 3
    # The stream/schema pairing is not overridable — it is the provider's.
    assert config.stream == "workout"
