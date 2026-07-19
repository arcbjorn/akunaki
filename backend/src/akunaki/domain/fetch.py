"""Pure types for connector fetch results.

No I/O, no HTTP client imports. A ``RawEnvelope`` carries the **exact** vendor
body plus redacted transport metadata, so the transport layer can persist a
faithful record of what the provider actually returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FetchFailure(StrEnum):
    """Why a fetch did not return a body."""

    UNAUTHORIZED = "unauthorized"
    """Token rejected; the connection needs re-authorization."""

    RATE_LIMIT = "rate_limit"
    """Vendor 429. Honor ``retry_after_seconds`` when present."""

    PROVIDER_ERROR = "provider_error"
    """Vendor 5xx or unexpected status; retrying may succeed."""

    TRANSPORT_ERROR = "transport_error"
    """Network failure before a response was read."""

    MALFORMED_RESPONSE = "malformed_response"
    """Body was not parseable as the declared content type."""

    @property
    def retryable(self) -> bool:
        """Whether retrying the same window could plausibly succeed.

        ``UNAUTHORIZED`` is excluded: it must flip the connection to
        ``needs_reauth`` rather than burn the job's attempt budget.
        """
        return self in {
            FetchFailure.RATE_LIMIT,
            FetchFailure.PROVIDER_ERROR,
            FetchFailure.TRANSPORT_ERROR,
        }


@dataclass(frozen=True, slots=True)
class RawEnvelope:
    """One exact vendor response page plus redacted transport metadata.

    ``payload_text`` is the body verbatim: it is hashed and persisted without
    reinterpretation, so a later normalizer sees exactly what arrived.
    """

    provider: str
    stream: str
    payload_text: str
    content_hash: str
    http_status: int
    content_type: str | None
    fetched_at: str
    request_meta: dict[str, str] = field(default_factory=dict)
    page_token: str | None = None
    next_page_token: str | None = None

    def __post_init__(self) -> None:
        if not self.content_hash:
            msg = "content_hash must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one fetch page.

    Exactly one of ``envelope`` or ``failure`` is meaningful; ``ok``
    distinguishes them.
    """

    envelope: RawEnvelope | None = None
    failure: FetchFailure | None = None
    retry_after_seconds: int | None = None

    @property
    def ok(self) -> bool:
        """True when a body was fetched."""
        return self.failure is None and self.envelope is not None
