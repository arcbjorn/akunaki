"""Retry classification and backoff policy are pure and deterministic."""

from __future__ import annotations

from datetime import timedelta

import pytest

from akunaki.domain.retry import (
    FailureKind,
    PermanentJobError,
    RetryPolicy,
    TransientJobError,
    classify_exception,
    error_class_of,
    redact_error_message,
)


class _BoomError(Exception):
    pass


def test_explicit_markers_classify_as_declared() -> None:
    assert classify_exception(PermanentJobError("bad payload")) is FailureKind.PERMANENT
    assert classify_exception(TransientJobError("vendor 503")) is FailureKind.TRANSIENT


def test_contract_errors_are_permanent() -> None:
    for exc in (ValueError("x"), TypeError("x"), KeyError("x"), NotImplementedError("x")):
        assert classify_exception(exc) is FailureKind.PERMANENT


def test_unknown_exceptions_default_to_transient() -> None:
    # An unanticipated bug must not burn the whole attempt budget at once.
    assert classify_exception(_BoomError("surprise")) is FailureKind.TRANSIENT


def test_backoff_grows_exponentially_and_caps() -> None:
    policy = RetryPolicy(
        base_delay=timedelta(seconds=1),
        max_delay=timedelta(seconds=60),
        jitter_ratio=0.0,
    )
    assert policy.delay_for_attempt(1) == timedelta(seconds=1)
    assert policy.delay_for_attempt(2) == timedelta(seconds=2)
    assert policy.delay_for_attempt(3) == timedelta(seconds=4)
    assert policy.delay_for_attempt(7) == timedelta(seconds=60)
    # Capped, not overflowing, at absurd attempt counts.
    assert policy.delay_for_attempt(9999) == timedelta(seconds=60)


def test_jitter_extends_within_ratio_and_respects_cap() -> None:
    policy = RetryPolicy(
        base_delay=timedelta(seconds=10),
        max_delay=timedelta(seconds=100),
        jitter_ratio=0.5,
    )
    assert policy.delay_for_attempt(1, jitter=0.0) == timedelta(seconds=10)
    assert policy.delay_for_attempt(1, jitter=0.5) == timedelta(seconds=12.5)
    # Jitter never pushes past max_delay.
    assert policy.delay_for_attempt(5, jitter=0.99) == timedelta(seconds=100)


def test_delay_never_below_one_second() -> None:
    # The durable lifecycle serializes at second precision; a subsecond delay
    # would round to immediate re-run.
    policy = RetryPolicy(base_delay=timedelta(milliseconds=10), jitter_ratio=0.0)
    assert policy.delay_for_attempt(1) >= timedelta(seconds=1)


def test_invalid_policy_and_arguments_are_rejected() -> None:
    with pytest.raises(ValueError, match="base_delay must be positive"):
        RetryPolicy(base_delay=timedelta(0))
    with pytest.raises(ValueError, match="max_delay must be >= base_delay"):
        RetryPolicy(base_delay=timedelta(seconds=10), max_delay=timedelta(seconds=1))
    with pytest.raises(ValueError, match="jitter_ratio"):
        RetryPolicy(jitter_ratio=1.5)
    with pytest.raises(ValueError, match="attempt_number must be >= 1"):
        RetryPolicy().delay_for_attempt(0)
    with pytest.raises(ValueError, match="jitter must be within"):
        RetryPolicy().delay_for_attempt(1, jitter=1.0)


def test_error_class_and_message_are_capped_and_phi_free() -> None:
    assert error_class_of(_BoomError("x")) == "_BoomError"
    assert redact_error_message(_BoomError("")) is None
    long = redact_error_message(_BoomError("y" * 900))
    assert long is not None
    assert len(long) == 500
