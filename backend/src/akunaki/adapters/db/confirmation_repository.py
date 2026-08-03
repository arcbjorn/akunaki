"""Confirmation persistence: issue, and atomically consume.

Consumption is the security-critical operation. The binding check and the
status flip run in **one** transaction with a conditional UPDATE, so two
concurrent executions of the same confirmation cannot both pass: exactly one
wins the CAS and the other sees zero affected rows and is rejected as already
consumed. Checking and then updating in separate statements would leave a
window in which a replay succeeds — which is precisely rule 4.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.sessions import hash_token
from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import ToolConfirmation
from akunaki.domain.confirmations import (
    ConfirmationBinding,
    ConfirmationRejection,
    ConfirmationStatus,
    check_confirmation,
)
from akunaki.domain.jobs import parse_utc_rfc3339, require_aware, to_utc_rfc3339

__all__ = ["ConfirmationRepository"]


class ConfirmationRepository:
    """Persist and redeem one-time tool-invocation confirmations."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def issue(
        self,
        *,
        confirmation_id: str,
        token: str,
        binding: ConfirmationBinding,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        """Record a pending confirmation for one specific call.

        Only the token's hash is stored; the caller keeps the raw token, which
        is the user's out-of-band handle to the authorization.
        """
        if not confirmation_id or not token:
            msg = "confirmation_id and token must be non-empty"
            raise ValueError(msg)

        with self._session_factory() as session, session.begin():
            session.add(
                ToolConfirmation(
                    id=confirmation_id,
                    tenant_id=binding.tenant_id,
                    user_id=binding.user_id,
                    token_hash=hash_token(token),
                    run_id=binding.run_id,
                    tool_name=binding.tool_name,
                    args_hash=binding.args_hash,
                    idempotency_key=binding.idempotency_key,
                    status=ConfirmationStatus.PENDING.value,
                    created_at=to_utc_rfc3339(require_aware(now, field_name="now")),
                    expires_at=to_utc_rfc3339(require_aware(expires_at, field_name="expires_at")),
                    consumed_at=None,
                )
            )

    def consume(
        self,
        *,
        token: str,
        requested: ConfirmationBinding,
        now: datetime,
    ) -> ConfirmationRejection | None:
        """Redeem a confirmation for ``requested``, or say why it does not authorize.

        Returns None when the execution is authorized **and** the confirmation
        has been marked consumed in the same transaction. Any other outcome
        leaves the row untouched.
        """
        if not token:
            return ConfirmationRejection.UNKNOWN
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        token_hash = hash_token(token)

        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(ToolConfirmation).where(ToolConfirmation.token_hash == token_hash)
            ).scalar_one_or_none()
            if row is None:
                return ConfirmationRejection.UNKNOWN

            stored = ConfirmationBinding(
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                run_id=row.run_id,
                tool_name=row.tool_name,
                args_hash=row.args_hash,
                idempotency_key=row.idempotency_key,
            )
            rejection = check_confirmation(
                stored=stored,
                status=ConfirmationStatus(row.status),
                expires_at=parse_utc_rfc3339(row.expires_at),
                requested=requested,
                now=now,
            )
            if rejection is not None:
                return rejection

            # Conditional CAS: only a row still pending may be consumed. The
            # read above and this write are two statements, so a second
            # redeemer that read the same ``pending`` row would pass the check;
            # re-asserting the status here means the loser affects zero rows.
            #
            # Not directly test-reachable on local libSQL: SQLite serializes
            # write transactions, so the loser's read blocks until the winner
            # commits and then observes ``consumed`` at the check instead. The
            # predicate is kept because that serialization is a property of this
            # store, not of the contract — a backend with snapshot reads (Turso
            # Cloud multi-client, per ADR 0003) reopens exactly this window.
            result = session.execute(
                update(ToolConfirmation)
                .where(
                    ToolConfirmation.id == row.id,
                    ToolConfirmation.status == ConfirmationStatus.PENDING.value,
                )
                .values(status=ConfirmationStatus.CONSUMED.value, consumed_at=now_s)
            )
            if affected_rows(result) != 1:
                return ConfirmationRejection.ALREADY_CONSUMED
            return None

    def cancel(self, *, tenant_id: str, confirmation_id: str, now: datetime) -> bool:
        """Withdraw a pending confirmation. False when there was none to cancel."""
        require_aware(now, field_name="now")
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(ToolConfirmation)
                .where(
                    ToolConfirmation.id == confirmation_id,
                    ToolConfirmation.tenant_id == tenant_id,
                    ToolConfirmation.status == ConfirmationStatus.PENDING.value,
                )
                .values(status=ConfirmationStatus.CANCELLED.value)
            )
            return affected_rows(result) == 1
