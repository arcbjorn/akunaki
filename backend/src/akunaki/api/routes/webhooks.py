"""Webhook ingress: verify, deduplicate, acknowledge, enqueue a refetch.

``POST /webhooks/{provider}/{connection_id}`` is **unauthenticated** — the
delivery comes from the vendor, not a browser session — so its trust comes
entirely from the HMAC-SHA256 signature over the exact request body, verified in
constant time. A verified delivery is recorded once (deduped per connection) and
a refetch is enqueued; the response is a fast acknowledgment, never the fetched
data. A signature or configuration failure is a generic rejection that discloses
nothing about which check failed.

The refetch is an ordinary ``connection.incremental_sync`` job, so the webhook
only *triggers* a pull — it never trusts the delivered body as data. Scheduled
reconciliation still covers any missed delivery.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.google_push_verifier import GooglePushVerifier
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.webhook_inbox_repository import WebhookInboxRepository
from akunaki.api.app import get_session_factory
from akunaki.config import Settings
from akunaki.domain.jobs import INCREMENTAL_SYNC_JOB_TYPE
from akunaki.domain.webhook_verification import HMAC_PROVIDERS, verify_hmac_signature

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_GOOGLE_HEALTH = "google_health"

# The signature header each HMAC provider sends. A provider absent here has no
# verifiable webhook path.
_SIGNATURE_HEADERS = {
    "oura": "x-oura-signature",
    "polar": "polar-webhook-signature",
}


class WebhookAck(BaseModel):
    """A fast acknowledgment. Never carries the delivered body or fetched data."""

    status: str


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _webhook_enabled(provider: str, settings: Settings) -> bool:
    """Whether a provider has a fully-configured, verifiable webhook path."""
    if provider in HMAC_PROVIDERS:
        return settings.webhook_secret(provider) is not None
    if provider == _GOOGLE_HEALTH:
        return bool(
            settings.google_health_push_audience.strip()
            and settings.google_health_push_service_account.strip()
        )
    return False


def _verify(
    *, provider: str, request: Request, body: bytes, settings: Settings, now: datetime
) -> bool:
    """Verify a delivery per its provider's scheme. Assumes it is enabled.

    HMAC providers verify the signature over the exact body; Google Health
    verifies the Google-signed push OIDC token in the Authorization header.
    """
    if provider in HMAC_PROVIDERS:
        secret = settings.webhook_secret(provider)
        signature = request.headers.get(_SIGNATURE_HEADERS[provider], "")
        return secret is not None and verify_hmac_signature(
            secret=secret, body=body, provided_signature=signature
        )
    # Google Health: a Bearer JWT in the Authorization header, Google-signed.
    authorization = request.headers.get("authorization", "")
    token = authorization[len("Bearer ") :] if authorization.startswith("Bearer ") else ""
    verifier = GooglePushVerifier(
        expected_audience=settings.google_health_push_audience,
        expected_service_account=settings.google_health_push_service_account,
    )
    return verifier.verify(bearer_token=token, now=now)


@router.post("/{provider}/{connection_id}", response_model=WebhookAck)
async def receive(
    provider: str,
    connection_id: str,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(_settings)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    delivery_id: Annotated[str | None, Header(alias="x-delivery-id")] = None,
) -> WebhookAck:
    """Verify and record a webhook delivery, then enqueue a refetch."""
    response.headers["Cache-Control"] = "no-store"

    # A provider with no configured verification path is an indistinguishable
    # 404 — ingress reveals nothing about which providers *could* be enabled.
    if not _webhook_enabled(provider, settings):
        raise HTTPException(status_code=404, detail={"code": "no_webhook"})

    body = await request.body()
    now = datetime.now(UTC)
    if not _verify(provider=provider, request=request, body=body, settings=settings, now=now):
        # One generic 401: never disclose whether the connection exists.
        raise HTTPException(status_code=401, detail={"code": "invalid_signature"})

    # Resolve the owning tenant/provider only after verification passes, so an
    # unverified request cannot probe which connections exist.
    connection = ConnectionRepository(session_factory).get_connection(connection_id=connection_id)
    if connection is None or connection.provider.value != provider:
        raise HTTPException(status_code=401, detail={"code": "invalid_signature"})

    # Dedupe on the vendor delivery id when present, else a hash of the body.
    dedupe_key = delivery_id or hashlib.sha256(body).hexdigest()
    inbox = WebhookInboxRepository(session_factory)
    record = inbox.record_delivery(
        inbox_id=str(uuid.uuid4()),
        tenant_id=connection.tenant_id,
        connection_id=connection_id,
        provider=provider,
        dedupe_key=dedupe_key,
        delivery_id=delivery_id,
        # Redacted metadata only — never the signature or any secret.
        headers_meta={"content_type": request.headers.get("content-type", "")},
        now=now,
    )
    if record.is_duplicate:
        # A vendor redelivery: already recorded, so acknowledge without a second
        # refetch. Idempotent by design.
        return WebhookAck(status="duplicate")

    # Trigger a refetch. Idempotency-keyed so concurrent deliveries collapse.
    JobRepository(session_factory).enqueue_job(
        job_id=str(uuid.uuid4()),
        tenant_id=connection.tenant_id,
        job_type=INCREMENTAL_SYNC_JOB_TYPE,
        payload_json=f'{{"connection_id":"{connection_id}"}}',
        now=now,
        idempotency_key=f"webhook_refetch:{connection_id}:{dedupe_key}",
    )
    inbox.mark_enqueued(inbox_id=record.inbox_id)
    return WebhookAck(status="accepted")


__all__ = ["router"]
