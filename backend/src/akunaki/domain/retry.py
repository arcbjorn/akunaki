"""Pure retry classification and backoff policy for durable job execution.

No I/O, no SQLAlchemy, no framework imports. The durable repository schedules
a retry using an exact caller-provided delay; deciding that delay and whether a
failure is retryable at all is this module's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

# Deterministic base/cap for exponential backoff. Jitter is supplied by the
# caller as a unit fraction so the policy itself stays pure and testable.
DEFAULT_BASE_DELAY = timedelta(seconds=1)
DEFAULT_MAX_DELAY = timedelta(minutes=15)
DEFAULT_JITTER_RATIO = 0.2

_MAX_ERROR_CLASS_LENGTH = 100
_MAX_REDACTED_ERROR_MESSAGE_LENGTH = 500


class FailureKind(StrEnum):
    """Why an attempt failed, which decides retryability."""

    TRANSIENT = "transient"
    """Vendor timeout, lock contention, 5xx: retry with backoff."""

    PERMANENT = "permanent"
    """Bad payload, unsupported type, 4xx contract violation: dead-letter now."""

    CANCELLED = "cancelled"
    """Cooperative shutdown mid-attempt: retry promptly without penalty."""


class PermanentJobError(Exception):
    """Handler failure that must never be retried."""


class TransientJobError(Exception):
    """Handler failure that should be retried with backoff."""


def classify_exception(exc: BaseException) -> FailureKind:
    """Map a handler exception to a failure kind.

    Unknown exception types are treated as ``TRANSIENT`` so that an
    unanticipated bug does not silently burn a job's attempt budget in one
    shot; the max-attempts ceiling still bounds total retries.
    """
    if isinstance(exc, PermanentJobError):
        return FailureKind.PERMANENT
    if isinstance(exc, TransientJobError):
        return FailureKind.TRANSIENT
    if isinstance(exc, ValueError | TypeError | KeyError | NotImplementedError):
        return FailureKind.PERMANENT
    return FailureKind.TRANSIENT


def error_class_of(exc: BaseException) -> str:
    """Return a PHI-free, length-capped error class label for an exception."""
    name = type(exc).__qualname__ or "Exception"
    return name[:_MAX_ERROR_CLASS_LENGTH]


def redact_error_message(exc: BaseException) -> str | None:
    """Return a length-capped message with no interpolated payload values.

    Health values and vendor payloads must never reach logs or durable failure
    records, so only the exception's own text is carried and it is truncated to
    the repository's documented cap.
    """
    text = str(exc).strip()
    if not text:
        return None
    return text[:_MAX_REDACTED_ERROR_MESSAGE_LENGTH]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with a cap and proportional jitter."""

    base_delay: timedelta = DEFAULT_BASE_DELAY
    max_delay: timedelta = DEFAULT_MAX_DELAY
    jitter_ratio: float = DEFAULT_JITTER_RATIO

    def __post_init__(self) -> None:
        if self.base_delay <= timedelta(0):
            msg = "base_delay must be positive"
            raise ValueError(msg)
        if self.max_delay < self.base_delay:
            msg = "max_delay must be >= base_delay"
            raise ValueError(msg)
        if not 0.0 <= self.jitter_ratio <= 1.0:
            msg = "jitter_ratio must be within [0.0, 1.0]"
            raise ValueError(msg)

    def delay_for_attempt(self, attempt_number: int, *, jitter: float = 0.0) -> timedelta:
        """Return the retry delay after ``attempt_number`` (1-based) failures.

        ``jitter`` is a unit fraction in [0.0, 1.0) supplied by the caller
        (typically ``random.random()``), keeping this function deterministic.
        The result is always at least one second because the durable lifecycle
        serializes timestamps at second precision.
        """
        if attempt_number < 1:
            msg = "attempt_number must be >= 1"
            raise ValueError(msg)
        if not 0.0 <= jitter < 1.0:
            msg = "jitter must be within [0.0, 1.0)"
            raise ValueError(msg)

        # Cap the exponent before multiplying so huge attempt counts cannot
        # overflow into an enormous intermediate timedelta.
        capped_exponent = min(attempt_number - 1, 32)
        raw_seconds = self.base_delay.total_seconds() * (2**capped_exponent)
        bounded = min(raw_seconds, self.max_delay.total_seconds())
        jittered = bounded * (1.0 + self.jitter_ratio * jitter)
        final = min(jittered, self.max_delay.total_seconds())
        return timedelta(seconds=max(1.0, final))
