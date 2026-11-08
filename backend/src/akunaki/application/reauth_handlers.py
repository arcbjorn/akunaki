"""Reauth probe: lift ``needs_reauth`` without the user when the grant still works.

``needs_reauth`` is meant to say the provider refused the **grant**. Until
2026-11-08 it was also set when a stale access token was refused after a
refresh that had merely failed to complete — and nothing ever re-checked: the
reconcile sweep skipped the status and manual syncs were refused, so one
transport error parked a working connection for good (Google, 2026-11-04; the
refresh token worked two seconds later on a sibling stream).

The probe asks the one question that settles it — *does the refresh token
still buy an access token?* — and acts on the answer:

- it does: persist the tokens, restore ``active``, and enqueue the
  connection's streams, so data resumes now rather than at the next sweep;
- ``invalid_grant``: the grant is dead. Re-stamp the status so the next probe
  waits a full interval, and leave re-consent as the only way out;
- anything else (transport, 5xx, a misconfigured client): no verdict.
  Re-stamp and try again next interval.

A provider's answer is a state, not a failure: the handler raises only for a
malformed job. A dead letter per probe of a dead grant would bury the real
ones, and the row the user needs is the connection's own ``last_error_class``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from akunaki.application.sync_handlers import enqueue_stream_syncs, parse_connection_payload
from akunaki.domain.connections import ConnectionStatus
from akunaki.domain.jobs import REAUTH_PROBE_JOB_TYPE, JobClaim
from akunaki.domain.retry import PermanentJobError
from akunaki.domain.secrets import SecretDecryptionError
from akunaki.domain.tokens import TokenExchangeFailure, merge_refreshed_tokens
from akunaki.ports.connections import ConnectionRepositoryPort
from akunaki.ports.jobs import JobEnqueuePort
from akunaki.ports.oauth_client import OAuthClientPort
from akunaki.ports.secrets import SecretSealerPort

logger = logging.getLogger("akunaki.reauth_probe")

__all__ = ["REAUTH_PROBE_JOB_TYPE", "ReauthProbeHandler"]


class ReauthProbeHandler:
    """Re-check a ``needs_reauth`` connection's grant by refreshing its token."""

    def __init__(
        self,
        *,
        connections: ConnectionRepositoryPort,
        sealer: SecretSealerPort,
        oauth_clients: Mapping[str, OAuthClientPort],
        jobs: JobEnqueuePort,
        new_id: Callable[[], str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connections = connections
        self._sealer = sealer
        self._oauth_clients = oauth_clients
        self._jobs = jobs
        self._new_id = new_id
        self._clock = clock

    def __call__(self, claim: JobClaim) -> None:
        """Execute one probe."""
        connection_id = parse_connection_payload(claim.payload_json)["connection_id"]
        now = self._clock()
        connection = self._connections.get_connection(connection_id=connection_id)
        if connection is None:
            msg = f"connection {connection_id!r} not found"
            raise PermanentJobError(msg)
        provider = connection.provider.value
        extra = {"connection_id": connection_id, "provider": provider}

        if connection.status is not ConnectionStatus.NEEDS_REAUTH:
            # Lifted in the meantime — a re-link, an earlier probe. Nothing to do.
            logger.info("reauth probe skipped: connection does not need reauth", extra=extra)
            return
        client = self._oauth_clients.get(provider)
        if client is None:
            logger.warning("reauth probe impossible: no OAuth client for provider", extra=extra)
            return
        stored = self._open_tokens(connection_id)
        if stored is None:
            return
        refresh_token = stored.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            logger.info("reauth probe impossible: no refresh token; re-consent only", extra=extra)
            return

        try:
            result = client.refresh(refresh_token=refresh_token, now=now)
        except Exception:
            logger.warning("reauth probe raised; no verdict", extra=extra, exc_info=True)
            self._restamp(connection_id, now=now, error_class="probe_failed")
            return

        if result.ok and result.tokens is not None:
            merged = merge_refreshed_tokens(stored, result.tokens)
            try:
                self._connections.replace_sealed_secret(
                    connection_id=connection_id,
                    sealed_secret=self._sealer.seal(
                        json.dumps(merged).encode(), aad=connection_id.encode()
                    ),
                    now=now,
                )
            except Exception:
                # A provider that rotates refresh tokens on every call (Oura)
                # has just invalidated the stored one, so the connection is no
                # longer known to be recoverable; leave the status honest.
                logger.exception(
                    "reauth probe refreshed the token but could not persist it", extra=extra
                )
                return
            self._connections.mark_status(
                connection_id=connection_id, status=ConnectionStatus.ACTIVE, now=now
            )
            try:
                enqueued = enqueue_stream_syncs(
                    self._jobs,
                    new_id=self._new_id,
                    connection_id=connection_id,
                    tenant_id=connection.tenant_id,
                    provider=provider,
                    now=now,
                )
            except KeyError:
                enqueued = 0
            logger.info(
                "connection recovered by token refresh", extra={**extra, "enqueued": enqueued}
            )
            return

        failure = result.failure
        self._restamp(connection_id, now=now, error_class=str(failure) if failure else "unknown")
        if failure is TokenExchangeFailure.INVALID_GRANT:
            logger.info("reauth probe: grant still refused; re-consent required", extra=extra)
        else:
            logger.warning(
                "reauth probe reached no verdict", extra={**extra, "failure": str(failure)}
            )

    def _open_tokens(self, connection_id: str) -> dict[str, object] | None:
        """Open the sealed token set, or None (logged) when there is nothing to open."""
        sealed = self._connections.get_sealed_secret(connection_id=connection_id)
        if sealed is None:
            logger.warning(
                "reauth probe impossible: connection has no stored credentials",
                extra={"connection_id": connection_id},
            )
            return None
        try:
            opened = self._sealer.open(sealed, aad=connection_id.encode())
        except SecretDecryptionError:
            logger.warning(
                "reauth probe impossible: stored credentials could not be opened",
                extra={"connection_id": connection_id},
            )
            return None
        stored: dict[str, object] = json.loads(opened)
        return stored

    def _restamp(self, connection_id: str, *, now: datetime, error_class: str) -> None:
        """Keep ``needs_reauth`` but move ``updated_at`` so the next probe waits an interval."""
        self._connections.mark_status(
            connection_id=connection_id,
            status=ConnectionStatus.NEEDS_REAUTH,
            now=now,
            error_class=error_class,
        )
