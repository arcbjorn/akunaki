"""User provisioning from a verified OIDC identity.

MVP tenancy is **one user per tenant**, so a first login provisions the tenant
and its user together, in one transaction. A returning login finds the existing
user by ``(oidc_issuer, oidc_subject)`` — never by email, which is mutable and
not an identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Tenant, User
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.oidc import VerifiedIdentity


@dataclass(frozen=True, slots=True)
class ProvisionedUser:
    """A user after login provisioning.

    ``created`` is True when this login provisioned a new tenant and user.
    """

    user_id: str
    tenant_id: str
    created: bool


class UserRepository:
    """Provision and look up users from verified OIDC identities."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def upsert_from_identity(
        self,
        *,
        identity: VerifiedIdentity,
        user_id: str,
        tenant_id: str,
        now: datetime,
    ) -> ProvisionedUser:
        """Return the existing user for this identity, or provision a new one.

        Identity is ``(oidc_issuer, oidc_subject)``. The supplied ``user_id``
        and ``tenant_id`` are used only when creating; a returning user keeps
        the ids it already has so sessions and facts stay attached.
        """
        for name, value in (
            ("user_id", user_id),
            ("tenant_id", tenant_id),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            existing = session.execute(
                select(User.id, User.tenant_id).where(
                    User.oidc_issuer == identity.issuer,
                    User.oidc_subject == identity.subject,
                )
            ).one_or_none()
            if existing is not None:
                found_user_id, found_tenant_id = existing
                # Refresh the email in case the IdP changed it, but never treat
                # email as identity or as a way to merge accounts.
                session.execute(
                    select(User).where(User.id == found_user_id)
                ).scalar_one().email = identity.email
                return ProvisionedUser(
                    user_id=found_user_id,
                    tenant_id=found_tenant_id,
                    created=False,
                )

            # First login: provision the tenant and its sole user together.
            session.add(
                Tenant(
                    id=tenant_id,
                    created_at=now_s,
                    status="active",
                    primary_timezone="UTC",
                    display_name=None,
                )
            )
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    oidc_issuer=identity.issuer,
                    oidc_subject=identity.subject,
                    email=identity.email,
                    created_at=now_s,
                )
            )
            return ProvisionedUser(user_id=user_id, tenant_id=tenant_id, created=True)
